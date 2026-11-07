"""m3b_mcp.feature 專用步驟(CHG-20261102-01 T8;使用者故事 1、2、3–7;AC-11;spec 8.1、8.2、9.1.4;決策 14、18)。

連線方式(CHG-20261107-03 T4 改為黑箱):
- 全部情境都打 `context.gw` 的 serve 子行程(`tests.blackbox.harness.Serve`;真實 HTTP,127.0.0.1 隨機埠)。
  HTTP 層(401/403、設定輸出、Host/Origin)以原生 httpx 斷言 HTTP body。
- MCP 工具(`tools/list`、`ask`、`list_routes`)與 `list_changed`:官方 SDK 用戶端在 `LoopThread` 上執行
  (`tests/blackbox/mcp.py`);GET 通知串流是否就緒經 `GET /__test__/mcp/sessions/{id}`。
- 遞迴情境的後半與停機情境:同一個 serve 以 `Serve.restart(graceful_timeout=…)` 重新啟動(寬限期是啟動期設定);
  stdio 以子行程 `SPILLWAY_CMD mcp-stdio` 經官方 SDK 的 stdio 用戶端。
- token:serve 以 `SPILLWAY_TEST_NO_TOKEN=1` 啟動,Background 經 `POST /api/settings/token/rotate` 產生;
  明文只存在 `context.m3b_token`(行程記憶體);雜湊以唯讀連線讀 `settings` 的 `access_token_sha256`。
- 設定輸出的網址:feature 文字的 `8787` 是預設埠;harness 以隨機埠啟動,步驟把它換成實際埠再比對(AM-12)。
- 收尾:登記到 `context.scenario_teardown`(environment.py 在停止 gw、刪除暫存目錄之前執行)。

Background 中與其他 feature 共用的文字(`一個空的閘道資料目錄與假 API 上游`、`假 CLI 執行檔在 PATH 最前面…`、
`成員 … 類型 "api" …`、`route … 的 target 含 …,採預設順序`)沿用既有步驟,不在此重複註冊。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import signal
import time
from typing import Any

import httpx
from behave import step, use_step_matcher

from tests.blackbox.db import ACCESS_TOKEN_KEY, setting
from tests.blackbox.harness import Serve
from tests.blackbox.mask import redact_tokens
from tests.blackbox.mcp import (
    INIT_BODY,
    MCP_ACCEPT,
    HeldSession,
    LoopThread,
    ask_route_enum,
    group_alive,
    mcp_http_session,
    pid_alive,
    stdio_session,
)
from tests.fakes.cli_upstream import assert_only_fakes
from tests.features.steps.common_steps import _parse_slots
from tests.features.steps.m3a_steps import _cli_body, _ids

use_step_matcher("re")

TOOLS = ("claude-code", "codex", "cursor", "gemini", "stdio")
ASK_PROMPT = "請檢查這段程式碼"
DEFAULT_PORT = 8787          # feature 文字中設定輸出網址的埠(AM-12:步驟換成 harness 的實際埠)


# ---------------------------------------------------------------- 驅動


def _teardown(context, fn) -> None:
    context.scenario_teardown.append(fn)


def _loop(context) -> LoopThread:
    loop = getattr(context, "m3b_loop", None)
    if loop is None:
        loop = context.m3b_loop = LoopThread()
        _teardown(context, loop.close)
    return loop


def _live(context) -> Serve:
    """本情境的 serve 子行程(Background 已啟動;收尾由 environment.py 停止)。"""
    gw = context.gw
    gw.ensure_started()
    return gw.serve


def _with_session(context, path: str, fn, *, token: Any = None) -> Any:
    lg = _live(context)
    tok = context.m3b_token if token is None else token

    async def go():
        async with mcp_http_session(lg.url(path), tok) as (s, init, _sid):
            return await fn(s, init)

    return _loop(context).run(go())


def _mcp_post(context, path: str, headers: dict[str, str]) -> httpx.Response:
    """原生 HTTP 的 initialize POST(經 context.gw;斷言 HTTP 狀態與 9.1.2 body)。"""
    resp = context.gw.post(path, json=INIT_BODY, headers={"Accept": MCP_ACCEPT, **headers})
    context.m3b_http = resp
    return resp


def _secrets(context) -> list[str]:
    return [t for t in (getattr(context, "m3b_token", None), getattr(context, "m3b_old_token", None)) if t]


def _token_hash(context) -> str | None:
    with context.gw.db() as conn:
        return setting(conn, ACCESS_TOKEN_KEY)


# ---------------------------------------------------------------- Background


@step(r"已產生存取 token")
def step_token(context):
    gw = context.gw
    gw.ensure_started()   # serve 以 NO_TOKEN 啟動(未簽發),經管理 API 產生
    context.m3b_token = gw.serve.rotate_token()
    tok = context.m3b_token
    assert tok and _token_hash(context) == hashlib.sha256(tok.encode()).hexdigest()


@step(r'CLI 成員 "([^"]+)",provider "(claude-cli)",額度類型 none')
def step_claude_member_none(context, dep_id, provider):
    gw = context.gw
    body = {**_cli_body(dep_id, provider),
            "budget": {"type": "none", "limit_amount": None, "window": "none"}}
    out = gw.ok(gw.create_member(body), 201)
    assert out["kind"] == "cli" and out["provider"] == provider and out["budget"]["type"] == "none", out


@step(r'群組 "([^"]+)" 的選擇器含 (.+)')
def step_group(context, name, members):
    gw = context.gw
    ids = _ids(members)
    out = gw.ok(gw.post("/api/groups", json={"name": name, "selector": {"all": [], "any": ids, "none": []}}),
                201)
    assert out["name"] == name, out


@step(r'task route "([^"]+)" 的 target 為群組 "([^"]+)",順序為 (slot .+)')
def step_task_route_group(context, name, group, order):
    gw = context.gw
    slots = _parse_slots(order)
    body = {"name": name, "target": {"group": group}, "mode": "task"}
    gw.ok(gw.post("/api/routes", json=body), 201)
    gw.routes[name] = body
    order_body = [[{"deployment": d, "weight": gw.members.get(d, {}).get("weight", "medium")} for d in s]
                  for s in slots]
    out = gw.ok(gw.put(f"/api/routes/{name}/order", json={"slots": order_body}), 200)
    assert [[e["deployment"] for e in s] for s in out["order"]] == slots, out


# ---------------------------------------------------------------- tools/list


@step(r'MCP 用戶端以正確 token 連線 "([^"]+)" 並列出工具')
def step_list_tools(context, path):
    async def fn(s, init):
        assert init.capabilities.tools is not None and init.capabilities.tools.listChanged is True, init
        return (await s.list_tools()).tools

    context.m3b_tools = _with_session(context, path, fn)


@step(r'工具 "ask" 的 route 選項只有 "([^"]+)"')
def step_enum_only(context, name):
    assert ask_route_enum(context.m3b_tools) == [name], ask_route_enum(context.m3b_tools)


@step(r'工具 "ask" 的 route 選項為 (.+)')
def step_enum_is(context, names):
    assert ask_route_enum(context.m3b_tools) == _ids(names), ask_route_enum(context.m3b_tools)


# ---------------------------------------------------------------- ask


@step(r'MCP 用戶端以正確 token 連線 "([^"]+)" 並以 route "([^"]+)" 呼叫 ask')
def step_ask(context, path, route):
    async def fn(s, _init):
        return await s.call_tool("ask", {"route": route, "prompt": ASK_PROMPT})

    context.m3b_result = _with_session(context, path, fn)


@step(r'ask 結果不是錯誤且 served_by 為 "([^"]+)"、slot 為 (\d+)')
def step_ask_ok(context, dep_id, slot):
    r = context.m3b_result
    assert r.isError is False, r.content
    sc = r.structuredContent
    assert sc["served_by"] == dep_id and sc["slot"] == int(slot), sc
    assert sc["text"], sc


@step(r"ask 結果帶 cost_usd 欄位")
def step_ask_cost(context):
    sc = context.m3b_result.structuredContent
    assert "cost_usd" in sc, sc
    assert sc["cost_usd"] is None or isinstance(sc["cost_usd"], (int, float)), sc


@step(r'帳本最新一列的 (route_name|error) 為 "([^"]+)"')
def step_ledger_latest(context, column, value):
    with context.gw.db() as conn:   # 唯讀;column 只會是 route_name 或 error(步驟 regex)
        rows = [dict(r) for r in conn.execute(f"SELECT id, {column} FROM usage_ledger")]
    assert rows, "帳本沒有任何列"
    latest = max(rows, key=lambda r: r["id"])
    assert latest[column] == value, latest


# ---------------------------------------------------------------- list_routes


@step(r'MCP 用戶端以正確 token 連線 "([^"]+)" 並呼叫 list_routes')
def step_list_routes(context, path):
    async def fn(s, _init):
        return await s.call_tool("list_routes", {})

    r = _with_session(context, path, fn)
    assert r.isError is False, r.content
    context.m3b_routes = r.structuredContent["routes"]


@step(r'list_routes 結果只含 route "([^"]+)"')
def step_list_routes_only(context, name):
    assert [r["name"] for r in context.m3b_routes] == [name], context.m3b_routes


@step(r'route "([^"]+)" 的 slot 與成員順序與 "GET /api/routes/([^"]+)" 的 order 相同')
def step_list_routes_order(context, name, api_name):
    gw = context.gw
    got = next(r for r in context.m3b_routes if r["name"] == name)
    order = gw.ok(gw.get(f"/api/routes/{api_name}"), 200)["order"]
    assert order, "前置條件:route 已手動排序"
    assert got["ordered"] == "manual", got
    mcp_slots = [[(m["id"], m["effort"], m["weight"]) for m in s["members"]] for s in got["slots"]]
    api_slots = [[(e["deployment"], e["effort"], e["weight"]) for e in s] for s in order]
    assert mcp_slots == api_slots, (mcp_slots, api_slots)
    assert [s["slot"] for s in got["slots"]] == list(range(1, len(order) + 1)), got


# ---------------------------------------------------------------- 401 / 403(HTTP 層)


@step(r'以不帶 Authorization 的請求送到 "([^"]+)"')
def step_no_auth(context, path):
    _mcp_post(context, path, {})


@step(r'以錯誤的 token 送到 "([^"]+)"')
def step_bad_token(context, path):
    _mcp_post(context, path, {"Authorization": "Bearer wrong-token-m3b"})


@step(r"以管理 API rotate 存取 token")
def step_rotate(context):
    gw = context.gw
    context.m3b_old_token = context.m3b_token
    context.m3b_token = gw.serve.rotate_token()      # POST /api/settings/token/rotate(斷言 200)
    assert context.m3b_token and context.m3b_token != context.m3b_old_token
    assert _token_hash(context) == hashlib.sha256(context.m3b_token.encode()).hexdigest()


@step(r'以 rotate 前的 token 送到 "([^"]+)"')
def step_old_token(context, path):
    _mcp_post(context, path, {"Authorization": f"Bearer {context.m3b_old_token}"})


@step(r'以正確 token 且 header "([^"]+)" 為 "([^"]*)" 送到 "([^"]+)"')
def step_token_and_header(context, name, value, path):
    _mcp_post(context, path, {"Authorization": f"Bearer {context.m3b_token}", name: value})


@step(r'以 header "([^"]+)" 為 "([^"]*)" 送 (GET|POST) 到 "([^"]+)"')
def step_header_request(context, name, value, method, path):
    context.m3b_http = context.gw.request(method, path, headers={name: value})


@step(r'HTTP 回應狀態為 (\d+) 且錯誤碼為 "([^"]+)"')
def step_http_status_code(context, status, code):
    r = context.m3b_http
    assert r.status_code == int(status), (r.status_code, redact_tokens(r.text, _secrets(context)))
    assert r.headers["content-type"].startswith("application/json"), r.headers
    body = r.json()
    assert body["error"]["code"] == code, body
    assert set(body["error"]) >= {"code", "message"}, body      # spec 9.1.2
    if int(status) == 401:
        assert r.headers.get("www-authenticate", "").lower().startswith("bearer"), r.headers
    leaked = any(tok in r.text for tok in _secrets(context))
    assert not leaked, "錯誤回應含 token 明文"


# ---------------------------------------------------------------- 遞迴(子行程 serve + mcp-stdio)


def _serve(context, graceful: float) -> Serve:
    """以寬限期 `graceful` 重新啟動同一個 serve(同一個 SPILLWAY_HOME、同一埠;收尾由 environment.py 停止)。"""
    gw = context.gw
    gw.start(gw.extra_paths, graceful_timeout=graceful)
    s = gw.serve
    assert s is not None
    context.m3b_serve = s
    return s


@step(r'以環境變數 SPILLWAY_DEPTH 為 "(\d+)" 執行 mcp-stdio --group ([^,\s]+),stdio 用戶端送出 initialize')
def step_stdio_depth(context, depth, group):
    s = getattr(context, "m3b_serve", None) or _serve(context, graceful=5.0)
    context.m3b_stdio = _loop(context).run(stdio_session(
        group=group, port=s.port, token=context.m3b_token, depth=depth,
        bridge_home=context.tmp / "bridge-home", fake_bin=context.fake_cli_bin), timeout=90)
    assert not (context.tmp / "bridge-home").exists(), "mcp-stdio 不可建立資料目錄"


@step(r'stdio 用戶端收到 JSON-RPC 錯誤且 error\.data\.code 為 "([^"]+)"')
def step_stdio_error(context, code):
    out = context.m3b_stdio
    assert out.init is None and out.error is not None, (out.init, out.error)
    assert isinstance(out.error.data, dict) and out.error.data.get("code") == code, out.error
    assert code in out.stderr, redact_tokens(out.stderr, _secrets(context))
    leaked = context.m3b_token in out.stderr
    assert not leaked, "mcp-stdio stderr 含 token 明文"


@step(r"mcp-stdio 的結束碼為 (\d+)")
def step_stdio_rc(context, rc):
    assert context.m3b_stdio.returncode == int(rc), context.m3b_stdio.returncode


# ---------------------------------------------------------------- list_changed(live_gateway)


@step(r'MCP 用戶端以正確 token 連線 "([^"]+)" 並開啟通知串流')
def step_open_stream(context, path):
    lg = _live(context)
    held = HeldSession(_loop(context), lg.url(path), context.m3b_token).open()
    _teardown(context, held.close)
    context.m3b_held = held
    # 決策 13:列過工具的 session 才登記;再輪詢 GET /__test__/mcp/sessions/{id} 到通知串流在伺服器端就緒
    context.m3b_tools = held.call(lambda s: _tools(s))
    lg.control.wait_stream_open(held.session_id, timeout=10.0)
    assert held.inbox == []


async def _tools(s) -> list:
    return (await s.list_tools()).tools


@step(r'以管理 API 建立 task route "([^"]+)",target 為群組 "([^"]+)"')
def step_create_route_live(context, name, group):
    lg = _live(context)
    with lg.http() as http:   # 同一個 serve(其 mcp_events 送出通知)
        r = http.post("/api/routes", json={"name": name, "target": {"group": group}, "mode": "task"})
    assert r.status_code == 201, r.text
    context.m3b_changed_at = time.monotonic()


@step(r'MCP 用戶端在 (\d+) 秒內收到 "notifications/tools/list_changed"')
def step_wait_list_changed(context, secs):
    held = context.m3b_held

    async def wait():
        while not held.inbox:
            await asyncio.sleep(0.02)

    context.m3b_loop.run(asyncio.wait_for(wait(), float(secs)), timeout=float(secs) + 5)
    assert time.monotonic() - context.m3b_changed_at <= float(secs)
    assert held.inbox[0].root.method == "notifications/tools/list_changed"


@step(r'重新列出工具後工具 "ask" 的 route 選項為 (.+)')
def step_relist(context, names):
    tools = context.m3b_held.call(lambda s: _tools(s))
    assert ask_route_enum(tools) == _ids(names), ask_route_enum(tools)


# ---------------------------------------------------------------- Host/Origin


@step(r"記錄目前存取 token 的雜湊")
def step_record_hash(context):
    context.m3b_hash = _token_hash(context)
    assert context.m3b_hash == hashlib.sha256(context.m3b_token.encode()).hexdigest()


@step(r"存取 token 的雜湊未改變")
def step_hash_unchanged(context):
    assert _token_hash(context) == context.m3b_hash


# ---------------------------------------------------------------- 設定輸出


@step(r'以管理 API 取得群組 "([^"]+)" 的 MCP 設定,工具依序為 (.+)')
def step_mcp_config(context, group, tools):
    gw = context.gw
    names = _ids(tools)
    assert tuple(names) == TOOLS, names
    context.m3b_group = group
    context.m3b_configs = {t: gw.ok(gw.get("/api/mcp/config", params={"group": group, "tool": t}), 200)
                           for t in names}


def _contents(cfg: dict[str, Any]) -> list[str]:
    return [v["content"] for v in cfg["variants"]]


@step(r'每份設定的 env 為 \["SPILLWAY_TOKEN"\] 且 content 含 "SPILLWAY_TOKEN"')
def step_config_env(context):
    for tool, cfg in context.m3b_configs.items():
        assert cfg["tool"] == tool and cfg["group"] == context.m3b_group, cfg
        assert cfg["env"] == ["SPILLWAY_TOKEN"], (tool, cfg["env"])
        for text in _contents(cfg):
            assert "SPILLWAY_TOKEN" in text, tool


@step(r"每份設定的 content 不含存取 token 明文")
def step_config_no_token(context):
    tok = context.m3b_token
    for tool, cfg in context.m3b_configs.items():
        assert tok not in json.dumps(cfg, ensure_ascii=False), tool


@step(r'除 "stdio" 外每份設定的 content 含 "([^"]+)"')
def step_config_url(context, url):
    # AM-12:feature 文字的網址帶預設埠 8787;serve 以 harness 選的隨機埠啟動(`free_port()` 一律不回 8787,
    # 施工審議 T4 F6),換成實際埠後比對(觀察方式改變)
    port = context.gw.serve.port
    assert f":{DEFAULT_PORT}/" in url, (url, port)
    url = url.replace(f"127.0.0.1:{DEFAULT_PORT}/", f"127.0.0.1:{port}/")
    for tool, cfg in context.m3b_configs.items():
        if tool == "stdio":
            continue
        assert url in cfg["variants"][0]["content"], tool


# ---------------------------------------------------------------- 停機(子行程 serve)


@step(r"閘道以子行程 serve 啟動且寬限期為 (\d+) 秒")
def step_serve_grace(context, secs):
    context.m3b_grace = float(secs)
    _serve(context, graceful=float(secs))


@step(r"假 claude 收到 prompt 後持續執行不結束")
def step_fake_hold(context):
    context.fake_cli.set("claude", "hold_until_release")


@step(r'MCP 用戶端以正確 token 連線 "([^"]+)" 並在背景以 route "([^"]+)" 呼叫 ask')
def step_background_ask(context, path, route):
    s = context.m3b_serve
    held = HeldSession(_loop(context), s.url(path), context.m3b_token).open()
    _teardown(context, held.close)
    context.m3b_held = held

    async def call():
        return await held.session.call_tool("ask", {"route": route, "prompt": ASK_PROMPT})

    context.m3b_future = context.m3b_loop.submit(call())


@step(r"假 claude 已啟動後對 serve 送出 SIGTERM")
def step_sigterm(context):
    (rec,) = context.fake_cli.wait_for_records("claude", 1, timeout=30)
    assert pid_alive(rec["pid"]), rec
    context.m3b_fake_rec = rec
    context.m3b_signalled = time.monotonic()
    context.m3b_serve.signal(signal.SIGTERM)      # 送到 serve 的行程群組(假 claude 另開 session,不受影響)


@step(r'ask 結果為錯誤且 structuredContent 的錯誤碼為 "([^"]+)"')
def step_ask_error(context, code):
    result = context.m3b_future.result(context.m3b_grace + 15)
    answered = time.monotonic() - context.m3b_signalled
    assert answered < context.m3b_grace, f"沒有在寬限內回應({answered:.1f}s)"
    assert result.isError is True, result.content            # 工具錯誤,不是 JSON-RPC error
    assert result.structuredContent["error"]["code"] == code, result.structuredContent
    s = context.m3b_serve
    try:
        s.wait_exit(context.m3b_grace + 15)
    finally:
        context.m3b_exit_elapsed = time.monotonic() - context.m3b_signalled
    context.m3b_held.close()


@step(r"serve 在寬限期內結束且沒有殘留的假 claude 行程")
def step_serve_exited(context):
    s = context.m3b_serve
    assert s.returncode == -signal.SIGTERM, (s.returncode, s.stderr_text()[-4000:])
    assert context.m3b_exit_elapsed <= context.m3b_grace, f"serve 未在寬限內結束({context.m3b_exit_elapsed:.2f}s)"
    rec = context.m3b_fake_rec
    assert not pid_alive(rec["pid"]) and not group_alive(rec["pid"]), "假 claude 或其孫行程仍在"
    assert len(context.fake_cli.records("claude")) == 1
    log = s.stderr_bytes().decode("utf-8", errors="replace")      # 原始 stderr(未遮罩)
    assert "Traceback" not in log
    leaked = any(t in log for t in _secrets(context))
    assert not leaked, "serve 日誌含 token 明文"
    assert_only_fakes()

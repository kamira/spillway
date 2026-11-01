"""m3a_cli.feature 專用步驟(CHG-20261101-03 T9;AC-1、AC-2、AC-12、AC-16/AC-17 CLI 版、NFR-6、spec 7.3、7.4)。

- 假 CLI:`tests/fakes/cli_upstream`(behave 環境在 `context.fake_cli_bin` 產生、`context.fake_cli` 控制);
  Background 把 `context.fake_cli_bin` 放到 PATH 最前面並斷言 PATH 上找不到真實 claude/codex
  (情境結束由 environment.py 還原 PATH)。真實 claude/codex 一律不執行。
- 「task 請求」:對 mode 為 task 的 route 送 `/v1/chat/completions`,另帶 `metadata.mode = "task"`。
- 與其他步驟檔共用的文字(`一個空的閘道資料目錄與假 API 上游`、`回應 header … 為 …`、`系統時間為 …`、
  `成員 … 的帳本列狀態為 …`、`route … 勾選預估額度檢查`、`管理 API 回應狀態為 … 且錯誤碼為 …`、
  `客戶端以 model … 送出 N 次請求`)直接沿用,不在此重複註冊。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from datetime import timedelta

from behave import step, use_step_matcher

from spillway.store import repo
from tests.fakes.cli_upstream import assert_only_fakes, is_fake
from tests.features.steps.common_steps import _parse_slots, _parse_utc, step_cooling_until
from tests.features.support.gateway import DEFAULT_MESSAGES

use_step_matcher("re")

CLAUDE_MODEL = "claude-sonnet-5-5"     # LiteLLM 本地價格表查得到(AC-16 precheck 需要定價)
CODEX_MODEL = "gpt-5.5"
TASK_META = {"mode": "task"}
PRICE_TOLERANCE = 0.0005
_IDS_RE = re.compile(r'"([^"]+)"')
SCENARIO_WORDS = {"額度用完": "quota", "成功": "ok", "限流": "rate_limit"}


def _ids(text: str) -> list[str]:
    found = _IDS_RE.findall(text)
    assert found, f"無法解析成員清單:{text}"
    return found


def _cli_body(dep_id: str, provider: str) -> dict:
    model = CLAUDE_MODEL if provider == "claude-cli" else CODEX_MODEL
    return {
        "id": dep_id, "model_name": dep_id, "kind": "cli", "provider": provider, "model": model,
        "params": {"timeout": 60},
        "capabilities": {"ctx": 272000, "tools": False, "vision": False,
                         "efforts": ["low", "medium", "high"]},
        "tags": [dep_id],
    }


def _task_chat(gw, model: str):
    return gw.chat(model, metadata=dict(TASK_META))


def _create_task_route(gw, name: str, ids: list[str], slots: list[list[str]] | None,
                       mode: str = "task") -> None:
    """target 為所列成員;`slots` 為 None 時不手動排序(4.3.2 預設順序)。"""
    if slots is not None:
        gw.create_route(name, slots, mode=mode)
        return
    body = {"name": name, "target": {"tags": {"all": [], "any": ids, "none": []}}, "mode": mode}
    gw.ok(gw.post("/api/routes", json=body), 201)
    gw.routes[name] = body
    got = gw.ok(gw.get(f"/api/routes/{name}"), 200)
    assert got["mode"] == mode, got


# ---------------------------------------------------------------- Background


@step(r"假 CLI 執行檔在 PATH 最前面且 PATH 上沒有真實的 claude 與 codex")
def step_fake_cli_on_path(context):
    bin_dir = str(context.fake_cli_bin)
    rest = [e for e in os.environ.get("PATH", "").split(os.pathsep) if e and e != bin_dir]
    os.environ["PATH"] = os.pathsep.join([bin_dir, *rest])
    resolved = assert_only_fakes()
    for name in ("claude", "codex"):
        hit = shutil.which(name)
        assert hit is not None and is_fake(hit), (name, hit)
        assert os.path.dirname(hit) == bin_dir, (name, hit)
    assert resolved == {"claude": str(context.fake_cli.path("claude")),
                        "codex": str(context.fake_cli.path("codex"))}, resolved


@step(r'CLI 成員 "([^"]+)",provider "(claude-cli)",額度 credit_pool (\d+) 美元每月重置')
def step_cli_member_credit(context, dep_id, provider, amount):
    gw = context.gw
    body = {**_cli_body(dep_id, provider),
            "budget": {"type": "credit_pool", "limit_amount": float(amount), "window": "monthly",
                       "reset_anchor": 1}}
    out = gw.ok(gw.create_member(body), 201)
    assert out["kind"] == "cli" and out["budget"]["type"] == "credit_pool", out
    row = gw.dashboard_row(dep_id)
    assert row["remaining"] == float(amount) and row["cooling"] is False, row


@step(r'CLI 成員 "([^"]+)",provider "(codex-cli)",額度類型 none')
def step_cli_member_none(context, dep_id, provider):
    gw = context.gw
    body = {**_cli_body(dep_id, provider),
            "budget": {"type": "none", "limit_amount": None, "window": "none"}}
    out = gw.ok(gw.create_member(body), 201)
    assert out["kind"] == "cli" and out["budget"]["type"] == "none", out


# ---------------------------------------------------------------- route


@step(r'task route "([^"]+)" 的 target 含 (.+),採預設順序')
def step_task_route_default(context, name, members):
    _create_task_route(context.gw, name, _ids(members), None)


@step(r'task route "([^"]+)" 的 target 含 (.+),順序為 (slot .+)')
def step_task_route_ordered(context, name, members, order):
    slots = _parse_slots(order)
    assert sorted(d for s in slots for d in s) == sorted(_ids(members)), (members, order)
    _create_task_route(context.gw, name, _ids(members), slots)


@step(r'task route "([^"]+)" 的 target 只含 "([^"]+)"')
def step_task_route_only(context, name, dep_id):
    _create_task_route(context.gw, name, [dep_id], [[dep_id]])


@step(r'route "([^"]+)" 的 target 含 (.+),採預設順序')
def step_chat_route_default(context, name, members):
    _create_task_route(context.gw, name, _ids(members), None, mode="chat")


# ---------------------------------------------------------------- 請求


@step(r'客戶端以 model "([^"]+)" 送出 (\d+) 次 task 請求')
def step_send_task(context, model, n):
    gw = context.gw
    gw.responses = []
    for _ in range(int(n)):
        _task_chat(gw, model)


@step(r'(\d+) 次回應的 header "([^"]+)" 皆為 "([^"]*)"')
def step_all_headers(context, n, name, value):
    resps = context.gw.responses
    assert len(resps) == int(n), len(resps)
    got = [(r.status_code, r.headers.get(name)) for r in resps]
    assert all(g == (200, value) for g in got), got


# ---------------------------------------------------------------- AC-12


@step(r'以管理 API 建立 CLI 成員 "([^"]+)",provider "([^"]+)"')
def step_admin_create_cli(context, dep_id, provider):
    gw = context.gw
    context.admin_resp = gw.create_member(_cli_body(dep_id, provider))


# ---------------------------------------------------------------- AC-16 CLI 版


def _estimate_entry(gw, route: str, dep_id: str) -> dict:
    out = gw.ok(gw.post("/api/estimate", json={"model": route, "messages": DEFAULT_MESSAGES,
                                               "metadata": dict(TASK_META)}), 200)
    for c in out["candidates"]:
        if c["deployment"] == dep_id:
            return {**c, "reason": None}
    for x in out["excluded"]:
        if x["deployment"] == dep_id:
            return x
    raise AssertionError(f"/api/estimate 沒有成員 {dep_id}:{out}")


@step(r'CLI 成員 "([^"]+)" 的 credit_pool 剩餘 ([0-9.]+) 美元且本次請求預估 P90 花費為 ([0-9.]+) 美元')
def step_cli_remaining_and_estimate(context, dep_id, remaining, est_cost):
    """前置條件:
    1. 本期已花費 = 上限 − remaining:同 route、同成員寫入 5 筆 ok 帳本(第一筆帶花費,其餘 0)。
    2. 這 5 筆的 tokens_out 依 LiteLLM 價格表(以裸 model 查價,同 estimate.py 的 CLI 退路)反推,
       讓 P90 花費 = est_cost(5.4.3:≥ 5 筆時取帳本 P90)。
    3. 經 `/api/estimate` 與 `/api/dashboard` 確認。
    """
    gw = context.gw
    remaining, est_cost = float(remaining), float(est_cost)
    route = next(iter(gw.routes))
    limit = float(gw.deployment(dep_id)["budget"]["limit_amount"])
    first = _estimate_entry(gw, route, dep_id)["estimate"]
    from spillway.adapters.base import load_litellm

    cin, cout = load_litellm().cost_per_token(model=CLAUDE_MODEL, prompt_tokens=first["tokens_in"],
                                              completion_tokens=1)
    tokens_out = round((est_cost - cin) / cout)
    now = gw.clock.now()
    with gw.db() as conn:
        for i in range(5):
            repo.insert_ledger(conn, repo.LedgerRow(
                ts=now - timedelta(hours=1, minutes=i), status="ok", route_name=route,
                deployment_id=dep_id, slot=1, tokens_in=first["tokens_in"], tokens_out=tokens_out,
                cost_usd=round(limit - remaining, 6) if i == 0 else 0.0))
    after = _estimate_entry(gw, route, dep_id)
    assert abs(after["estimate"]["cost_usd_p90"] - est_cost) < PRICE_TOLERANCE, after
    assert after.get("reason") == "skipped_precheck", after
    row = gw.dashboard_row(dep_id)
    assert abs(row["remaining"] - remaining) < 1e-6 and row["cooling"] is False, row


# ---------------------------------------------------------------- AC-17 CLI 版


@step(r'假 (claude|codex) 的情境設為(額度用完|成功|限流)')
def step_fake_cli_scenario(context, name, word):
    context.fake_cli.set(name, SCENARIO_WORDS[word])


@step(r'CLI 成員 "([^"]+)" 冷卻至 "([^"]+)"')
def step_cli_cooling(context, dep_id, when):
    step_cooling_until(context, dep_id, when)
    assert context.gw.deployment(dep_id)["cooling_reason"] == "quota_exhausted"


@step(r'假 (claude|codex) 的情境設為(額度用完|成功|限流)且系統時間推進至 "([^"]+)"')
def step_fake_cli_scenario_and_time(context, name, word, when):
    context.fake_cli.set(name, SCENARIO_WORDS[word])
    context.gw.clock.set(_parse_utc(when))


# ---------------------------------------------------------------- NFR-6


@step(r'成員 "([^"]+)" 的 max_concurrency 為 (\d+),假 claude 會等到放行訊號才回應')
def step_max_concurrency_hold(context, dep_id, n):
    gw = context.gw
    params = {**gw.members[dep_id]["body"].get("params", {}), "max_concurrency": int(n)}
    out = gw.update_member(dep_id, params=params)
    assert out["params"]["max_concurrency"] == int(n), out
    context.fake_cli.set("claude", "hold_until_release")
    context.add_cleanup(context.fake_cli.release, "claude")   # 失敗時也放行,不留等待中的假行程


async def _wait_started(fake_cli, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not fake_cli.records("claude"):
        assert time.monotonic() < deadline, "假 claude 沒有啟動"
        await asyncio.sleep(0.02)


@step(r'客戶端以 model "([^"]+)" 送出第 1 次 task 請求並等到假 claude 已開始')
def step_first_held(context, model):
    gw = context.gw
    gw.ensure_started()
    body = {"model": model, "messages": DEFAULT_MESSAGES, "metadata": dict(TASK_META)}

    async def spawn():
        return asyncio.ensure_future(gw.client.post("/v1/chat/completions", json=body))

    context.first_task = gw.runner.run(spawn())
    gw.runner.run(_wait_started(context.fake_cli))
    assert gw.app.state.cli_runner.in_flight("claude-sub") == 1


@step(r'客戶端以 model "([^"]+)" 送出第 2 次 task 請求')
def step_second(context, model):
    gw = context.gw
    context.second_resp = _task_chat(gw, model)
    assert not context.first_task.done(), "第 1 次請求不應在放行前完成"


@step(r"放行假 claude")
def step_release(context):
    gw = context.gw
    context.fake_cli.release("claude")

    async def finish():
        return await asyncio.wait_for(context.first_task, 30)

    context.first_resp = gw.runner.run(finish())


@step(r'第 1 次由 "([^"]+)" 處理、第 2 次由 "([^"]+)" 處理')
def step_first_second_served(context, first, second):
    for resp, want in ((context.first_resp, first), (context.second_resp, second)):
        assert resp.status_code == 200, resp.text
        assert resp.headers["X-Spillway-Served-By"] == want, resp.headers
    assert context.gw.app.state.cli_runner.in_flight("claude-sub") == 0


@step(r'帳本中 "([^"]+)" 只有 (\d+) 列')
def step_ledger_rows_for(context, dep_id, n):
    gw = context.gw
    items = gw.ledger(deployment=dep_id)["items"]
    assert len(items) == int(n), items
    assert len(context.fake_cli.records("claude")) == int(n)


# ---------------------------------------------------------------- codex


@step(r'成員 "([^"]+)" 最新一列帳本的 cost_usd 為 NULL 且 tokens_out 有值')
def step_cost_null(context, dep_id):
    gw = context.gw
    row = gw.latest_ledger(deployment=dep_id)
    assert row["status"] == "ok" and row["cost_usd"] is None, row
    assert isinstance(row["tokens_out"], int) and row["tokens_out"] > 0, row
    with gw.db() as conn:
        cost = conn.execute("SELECT cost_usd FROM usage_ledger WHERE id = ?", (row["id"],)).fetchone()[0]
    assert cost is None


# ---------------------------------------------------------------- 子行程環境與隔離旗標


@step(r'環境變數 (.+) 設為測試假值')
def step_env_fake_values(context, names):
    saved = {}
    for name in _ids(names):
        saved[name] = os.environ.get(name)
        os.environ[name] = f"fake-{name.lower()}-behave"

    def restore():
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    context.add_cleanup(restore)


@step(r'CLI 成員 "([^"]+)" 的 params\.extra_args 設為 (\[.*\])')
def step_extra_args(context, dep_id, raw):
    gw = context.gw
    extra = json.loads(raw)
    params = {**gw.members[dep_id]["body"].get("params", {}), "extra_args": extra}
    out = gw.update_member(dep_id, params=params)
    assert out["params"]["extra_args"] == extra, out


def _last_record(context, name: str) -> dict:
    recs = context.fake_cli.wait_for_records(name, 1, finished=True)
    return recs[-1]


def _assert_env(rec: dict, absent: list[str], depth: str) -> None:
    for var in absent:
        assert rec["env_present"][var] is False, (var, rec["env_present"])
    assert rec["spillway_depth"] == depth, rec


@step(r'假 claude 收到的環境沒有 (.+),且 "SPILLWAY_DEPTH" 為 "(\d+)"')
def step_claude_env(context, names, depth):
    assert os.environ.get("ANTHROPIC_API_KEY"), "前置條件:測試行程的環境應有假值"
    _assert_env(_last_record(context, "claude"), _ids(names), depth)


@step(r'假 claude 收到的參數含 "([^"]+)" 與 "([^"]+)",prompt 由 stdin 傳入')
def step_claude_args(context, a, b):
    rec = _last_record(context, "claude")
    argv = rec["argv"]
    assert a in argv and b in argv, argv
    assert argv[:1] == ["-p"], argv
    prompt_text = DEFAULT_MESSAGES[0]["content"]
    assert prompt_text in rec["stdin"], rec["stdin"]
    assert not any(prompt_text in x for x in argv), argv


@step(r'假 codex 收到的環境沒有 (.+),且 "SPILLWAY_DEPTH" 為 "(\d+)"')
def step_codex_env(context, names, depth):
    assert os.environ.get("OPENAI_API_KEY"), "前置條件:測試行程的環境應有假值"
    _assert_env(_last_record(context, "codex"), _ids(names), depth)


@step(r'假 codex 收到的參數含 "([^"]+)" 與 "([^"]+)",倒數第二個參數為 "([^"]+)"')
def step_codex_args(context, flag, value, sep):
    argv = _last_record(context, "codex")["argv"]
    pairs = list(zip(argv, argv[1:]))
    assert (flag, value) in pairs, argv
    assert argv[-2] == sep, argv
    assert argv[-1].strip().endswith(DEFAULT_MESSAGES[0]["content"]), argv

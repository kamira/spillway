"""M3b 操作驗收的自動部分(CHG-20261102-01「### Acceptance operation」;T8;AM-17;macOS)。

拆成 4 個測試函式(認證/工具、list_changed、stdio、停機),各自:
- 暫存 `SPILLWAY_HOME`;PATH 只含假 CLI(`safe_subprocess_env`,斷言找不到真實 claude/codex);
- 子行程 `python -m spillway init` 取得 token(stdout 只印 token;明文只存在測試行程記憶體);
- 子行程 `python -m spillway serve --port <隨機>`(`SPILLWAY_TEST_GRACEFUL_TIMEOUT=2`)與子行程假 API 上游;
- 以管理 API 建立 claude-sub(claude-cli)、deepseek-api(api)、群組 `coding`、task route `review`
  (target 群組 coding,slot 1 claude-sub、slot 2 deepseek-api)與 chat route `chat-x`(不屬 coding)。
MCP 一律以官方 SDK 用戶端實際連線(Streamable HTTP 對子行程 serve;stdio 對子行程 `mcp-stdio`)。

產物(`artifacts/m3b/`,不進版控):
- `http-log.jsonl`:測試行程對閘道的每一筆 HTTP 請求(管理 API、MCP 的 POST/GET/DELETE)一行,只有
  `method`、`path`(不含 query)、`status`、`elapsed_ms` 四鍵——MCP 用戶端的 `http_client` 掛 httpx event hook
  寫入;不記 header 與 body。
- `operation-results.json`:各函式的觀察值(不含任何秘密)與 M3 閘門對照表(`m3_gates`)。
GET 通知串流「已在伺服器端就緒」:子行程無法讀 SDK 狀態(`sdk_compat.get_stream_open` 只在行程內可用),
以「改一次 route → 收到 `tools/list_changed`」證明(同 tests/integration/mcp/test_mcp_shutdown.py)。
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.acceptance import procs
from tests.fakes.cli_upstream import FakeCli, assert_only_fakes, install
from tests.helpers import redact_tokens
from tests.features.support.mcp_proc import (
    INIT_BODY,
    MCP_ACCEPT,
    ServeProc,
    ask_route_enum,
    child_env,
    group_alive,
    is_list_changed,
    mcp_http_session,
    pid_alive,
    raw_get_stream,
    stdio_session,
)

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not procs.HAS_LSOF or shutil.which("pgrep") is None,
                       reason="需要 lsof 與 pgrep(macOS 驗收平台)"),
]

REPO_ROOT = procs.REPO_ROOT
ARTIFACTS = REPO_ROOT / "artifacts" / "m3b"
HTTP_LOG = ARTIFACTS / "http-log.jsonl"
RESULTS = ARTIFACTS / "operation-results.json"
LOG_KEYS = {"method", "path", "status", "elapsed_ms"}
GRACE = 2.0                 # SPILLWAY_TEST_GRACEFUL_TIMEOUT
GET_CLOSE_LIMIT = 1.0       # SIGTERM 後 GET 通知串流「立即」結束的上限(秒)
LIST_CHANGED_WAIT = 5.0
CLAUDE, API, GROUP = "claude-sub", "deepseek-api", "coding"
TOOLS = ("claude-code", "codex", "cursor", "gemini", "stdio")
PROMPT = "請檢查這段程式碼"
# 本模組產生過的全部 token 明文(只在測試行程記憶體);模組結束時掃描 artifacts/m3b/ 每個檔案,
# 並用於遮掉斷言訊息中的日誌/回應文字
TOKENS: list[str] = []

# M3 閘門對照表(CHG「Acceptance operation」;結果記入 ACC-20261102-01)
M3_GATES = [
    {"gate": ["AC-1", "AC-2", "AC-12", "AC-16", "AC-17", "NFR-6"],
     "source": ["tests/features/m3a_cli.feature", "tests/acceptance/test_m3a_operation.py",
                "ACC-20261101-02 擁有者冒煙結果"],
     "note": "沿用;本筆全量回歸(pytest -q、behave tests/features)"},
    {"gate": ["AC-9"],
     "source": ["擁有者步驟 ④(4 種工具各列出並呼叫 ask;Gemini 依 UD-1:headers 或 stdio variant 任一成功)",
                "自動部分:operation-results.json auth_tools.mcp_config(5 種設定輸出可解析、不含 token)"],
     "note": "待擁有者回報;ACC 記錄 Gemini 採用路徑與 headers 結果"},
    {"gate": ["AC-11"],
     "source": ["T2 L1:tests/integration/mcp/test_mcp_auth.py", "T4 L1:tests/integration/test_mcp_stdio.py",
                "operation:auth_tools.depth_header、stdio.depth_rejected", "tests/features/m3b_mcp.feature 遞迴情境"],
     "note": "本筆自動驗證"},
]


# ---------------------------------------------------------------- 產物


class HttpLog:
    """只記四鍵;同步與非同步 httpx client 都用(event hooks)。"""

    _lock = threading.Lock()

    @staticmethod
    def _line(resp: httpx.Response) -> None:
        t0 = resp.request.extensions.get("m3b_t0")
        ms = round((time.perf_counter() - t0) * 1000, 2) if t0 is not None else None
        line = {"method": resp.request.method, "path": resp.request.url.path, "status": resp.status_code,
                "elapsed_ms": ms}
        with HttpLog._lock:
            HTTP_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(HTTP_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")

    @staticmethod
    def sync_hooks() -> dict[str, list[Any]]:
        def req(r: httpx.Request) -> None:
            r.extensions["m3b_t0"] = time.perf_counter()

        return {"request": [req], "response": [HttpLog._line]}

    @staticmethod
    def async_hooks() -> dict[str, list[Any]]:
        async def req(r: httpx.Request) -> None:
            r.extensions["m3b_t0"] = time.perf_counter()

        async def resp(r: httpx.Response) -> None:
            HttpLog._line(r)

        return {"request": [req], "response": [resp]}


def record(key: str, value: Any) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(RESULTS.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        data = {}
    data.update({"platform": "macOS(Linux、Windows 未涵蓋)", "m3_gates": M3_GATES, key: value})
    RESULTS.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@pytest.fixture(scope="module", autouse=True)
def _fresh_artifacts():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    for p in (HTTP_LOG, RESULTS):
        p.unlink(missing_ok=True)
    TOKENS.clear()
    yield
    # 1) http-log.jsonl:存在、非空、每行恰好四鍵,且含 MCP 請求
    assert HTTP_LOG.exists(), "沒有產生 http-log.jsonl"
    lines = [json.loads(ln) for ln in HTTP_LOG.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "http-log.jsonl 是空的"
    bad = [sorted(ln) for ln in lines if set(ln) != LOG_KEYS]
    assert not bad, bad
    assert any(str(ln["path"]).startswith("/mcp") for ln in lines), "http-log.jsonl 沒有 MCP 請求"
    # 2) operation-results.json 寫入之後,artifacts/m3b/ 下每個檔案都不含任何 token 明文
    assert RESULTS.exists(), "沒有產生 operation-results.json"
    assert TOKENS, "前置條件:至少產生過一個 token"
    leaked = sorted({f for tok in TOKENS for f in _files_containing(ARTIFACTS, tok)})
    assert not leaked, f"含 token 明文的產物:{leaked}"


def _files_containing(root: Path, needle: str) -> list[str]:
    data = needle.encode()
    return [str(p) for p in root.rglob("*") if p.is_file() and data in p.read_bytes()]


# ---------------------------------------------------------------- 環境


class Op:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.fake = FakeCli(self.home, install(tmp_path / "fake-cli-bin"))
        self.started = time.time()
        self.extra_env = {"SPILLWAY_TEST_KEY": procs.TEST_KEY}
        env = child_env(self.home, fake_bin=self.fake.bin_dir, extra=self.extra_env)
        assert assert_only_fakes(env["PATH"]) == {"claude": str(self.fake.path("claude")),
                                                  "codex": str(self.fake.path("codex"))}
        r = subprocess.run([sys.executable, "-m", "spillway", "init"], env=env, cwd=str(REPO_ROOT),
                           capture_output=True, text=True, timeout=120)
        self.tokens: list[str] = []
        self.token = r.stdout.strip()
        if self.token:
            self.register(self.token)
        assert r.returncode == 0, redact_tokens(r.stderr, TOKENS)
        one_line = bool(self.token) and len(r.stdout.splitlines()) == 1
        assert one_line, "init 的 stdout 應只有一行 token"
        in_stderr = self.token in r.stderr
        assert not in_stderr, "init 的 stderr 含 token 明文"
        self.serves: list[ServeProc] = []
        self.http: httpx.Client = None  # type: ignore[assignment] — serve 就緒後才建立
        self.fake_proc, self.fake_base = procs.start_fake_upstream(tmp_path)
        try:
            self.serve = self.start_serve()
        except BaseException:
            self.close()
            raise
        self.http = httpx.Client(base_url=self.serve.url, trust_env=False, timeout=60,
                                 event_hooks=HttpLog.sync_hooks())

    def register(self, token: str) -> None:
        self.tokens.append(token)
        TOKENS.append(token)

    # -- 行程
    def start_serve(self) -> ServeProc:
        s = ServeProc(self.home, fake_bin=self.fake.bin_dir, graceful=GRACE,
                      log_path=self.tmp / f"serve-{len(self.serves)}.log", extra_env=self.extra_env,
                      secrets=TOKENS)
        self.serves.append(s)
        s.wait_ready()
        return s

    def close(self) -> None:
        if self.http is not None:
            self.http.close()
        for s in self.serves:
            s.stop()
        procs.stop(self.fake_proc)
        for rec in self.fake.records():
            if not rec.get("finished") and rec.get("started_at", 0) >= self.started and group_alive(rec["pid"]):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(rec["pid"], signal.SIGKILL)
        shutil.rmtree(self.fake.control_dir, ignore_errors=True)

    # -- HTTP
    def ok(self, r: httpx.Response, status: int = 200) -> Any:
        assert r.status_code == status, \
            f"{r.request.method} {r.request.url.path} → {r.status_code} {redact_tokens(r.text, TOKENS)}"
        return r.json() if r.content else None

    def seed(self) -> None:
        caps = {"ctx": 272000, "tools": False, "vision": False, "efforts": ["low", "medium", "high"]}
        self.ok(self.http.post("/api/deployments", json={
            "id": CLAUDE, "model_name": CLAUDE, "kind": "cli", "provider": "claude-cli",
            "model": "claude-sonnet-5-5", "params": {"timeout": 60}, "tags": [CLAUDE], "capabilities": caps,
            "budget": {"type": "credit_pool", "limit_amount": 10.0, "window": "monthly", "reset_anchor": 1}}), 201)
        self.ok(self.http.post("/api/deployments", json={
            "id": API, "model_name": API, "kind": "api", "provider": "openai", "model": "gpt-4o-mini",
            "params": {"api_base": self.fake_base + "/v1"}, "credential_ref": "env:SPILLWAY_TEST_KEY",
            "tags": [API], "capabilities": {"ctx": 128000, "tools": True, "vision": False, "efforts": []}}), 201)
        self.ok(self.http.post("/api/groups", json={
            "name": GROUP, "selector": {"all": [], "any": [CLAUDE, API], "none": []}}), 201)
        self.ok(self.http.post("/api/routes", json={"name": "review", "target": {"group": GROUP},
                                                    "mode": "task"}), 201)
        self.ok(self.http.put("/api/routes/review/order",
                              json={"slots": [[{"deployment": CLAUDE}], [{"deployment": API}]]}))
        self.ok(self.http.post("/api/routes", json={
            "name": "chat-x", "mode": "chat", "target": {"tags": {"all": [], "any": [API], "none": []}}}), 201)

    def mcp(self, group: str = GROUP, *, token: str | None = None, **kw: Any):
        return mcp_http_session(f"{self.serve.url}/mcp/{group}", token or self.token,
                                event_hooks=HttpLog.async_hooks(), **kw)

    def raw_mcp(self, headers: dict[str, str], group: str = GROUP) -> httpx.Response:
        return self.http.post(f"/mcp/{group}", json=INIT_BODY, headers={"Accept": MCP_ACCEPT, **headers})

    def token_hash(self) -> str:
        c = sqlite3.connect(str(self.home / "spillway.db"))
        try:
            return c.execute("SELECT value FROM settings WHERE key = 'access_token_sha256'").fetchone()[0]
        finally:
            c.close()

    def ledger(self) -> list[dict[str, Any]]:
        return self.ok(self.http.get("/api/ledger", params={"page_size": 500}))["items"]

    def assert_no_token_leak(self) -> dict[str, list[str]]:
        leaks: dict[str, list[str]] = {}
        logs = "".join(s.log_text() for s in self.serves)
        for i, tok in enumerate(self.tokens):
            found = _files_containing(self.home, tok) + _files_containing(ARTIFACTS, tok)
            if tok in logs:
                found.append("serve log")
            leaks[f"token{i}"] = found
        assert all(not v for v in leaks.values()), leaks
        return leaks


@pytest.fixture
def op(tmp_path):
    o = Op(tmp_path)
    try:
        o.seed()
        yield o
    finally:
        o.close()
    assert all(s.proc.poll() is not None for s in o.serves) and o.fake_proc.poll() is not None


def _error(r: httpx.Response) -> tuple[int, str]:
    assert r.headers["content-type"].startswith("application/json"), r.headers
    return r.status_code, r.json()["error"]["code"]


def _parse(v: dict[str, Any]) -> Any:
    if v["format"] == "json":
        return json.loads(v["content"])
    if v["format"] == "toml":
        return tomllib.loads(v["content"])
    return shlex.split(v["content"])


# ---------------------------------------------------------------- 1. 認證/工具


async def test_m3b_operation_auth_and_tools(op: Op):
    obs: dict[str, Any] = {}
    async with op.mcp() as (s, init, _sid):
        obs["list_changed_capability"] = init.capabilities.tools.listChanged
        tools = (await s.list_tools()).tools
        obs["tools"] = [t.name for t in tools]
        obs["ask_route_enum"] = ask_route_enum(tools)
        ask = await s.call_tool("ask", {"route": "review", "prompt": PROMPT})
        lr = await s.call_tool("list_routes", {})
        qs = await s.call_tool("quota_status", {})
        dash = {d["id"]: d for d in op.ok(op.http.get("/api/dashboard"))["deployments"]}
    assert obs["list_changed_capability"] is True
    assert obs["tools"] == ["ask", "list_routes", "quota_status"]
    assert obs["ask_route_enum"] == ["review"]                       # 不含 chat-x

    sc = ask.structuredContent
    obs["ask"] = {"is_error": ask.isError, "served_by": sc.get("served_by"), "slot": sc.get("slot"),
                  "has_cost_usd": "cost_usd" in sc}
    assert ask.isError is False and sc["served_by"] == CLAUDE and sc["slot"] == 1 and "cost_usd" in sc, sc
    row = op.ledger()[0]
    obs["ask_ledger"] = {k: row[k] for k in ("route_name", "deployment_id", "status", "slot")}
    assert obs["ask_ledger"] == {"route_name": "review", "deployment_id": CLAUDE, "status": "ok", "slot": 1}

    order = op.ok(op.http.get("/api/routes/review"))["order"]
    (route,) = lr.structuredContent["routes"]
    mcp_order = [[m["id"] for m in s_["members"]] for s_ in route["slots"]]
    obs["list_routes"] = {"name": route["name"], "ordered": route["ordered"], "slots": mcp_order,
                          "matches_admin": mcp_order == [[e["deployment"] for e in s_] for s_ in order]}
    assert route["name"] == "review" and route["ordered"] == "manual" and obs["list_routes"]["matches_admin"]

    members = {m["id"]: m for m in qs.structuredContent["members"]}
    fields = ("remaining", "cooling_until", "cooling_reason", "budget_type", "window")
    obs["quota_status"] = {"members": sorted(members),
                           "matches_dashboard": all({f: members[i][f] for f in fields}
                                                    == {f: dash[i][f] for f in fields} for i in members)}
    assert sorted(members) == [CLAUDE, API] and obs["quota_status"]["matches_dashboard"], (members, dash)

    # ---- 拒絕:錯誤 token、depth、跨站 Origin、非本機 Host
    obs["bad_token"] = _error(op.raw_mcp({"Authorization": "Bearer wrong-token-m3b"}))
    obs["depth_header"] = _error(op.raw_mcp({"Authorization": f"Bearer {op.token}", "X-Spillway-Depth": "1"}))
    before = op.token_hash()
    obs["origin_rotate"] = _error(op.http.post("/api/settings/token/rotate",
                                               headers={"Origin": "http://evil.example"}))
    obs["origin_rotate_token_unchanged"] = op.token_hash() == before
    obs["host"] = _error(op.raw_mcp({"Authorization": f"Bearer {op.token}", "Host": "evil.example:8787"}))
    assert obs["bad_token"] == (401, "unauthorized")
    assert obs["depth_header"] == (403, "recursion_blocked")
    assert obs["origin_rotate"] == (403, "origin_not_allowed") and obs["origin_rotate_token_unchanged"]
    assert obs["host"] == (403, "host_not_allowed")

    # ---- rotate 後舊 token 被拒、新 token 可用
    old = op.token
    new = op.ok(op.http.post("/api/settings/token/rotate"))["token"]
    op.token = new
    op.register(new)
    rotated = new != old and op.token_hash() == hashlib.sha256(new.encode()).hexdigest()
    assert rotated, "rotate 應回傳新 token 且資料庫只存新 token 的雜湊"
    obs["old_token_after_rotate"] = _error(op.raw_mcp({"Authorization": f"Bearer {old}"}))
    obs["new_token_after_rotate"] = op.raw_mcp({"Authorization": f"Bearer {new}"}).status_code
    assert obs["old_token_after_rotate"] == (401, "unauthorized") and obs["new_token_after_rotate"] == 200

    # ---- 設定輸出 5 種工具
    cfgs: dict[str, Any] = {}
    for tool in TOOLS:
        cfg = op.ok(op.http.get("/api/mcp/config", params={"group": GROUP, "tool": tool}))
        text = json.dumps(cfg, ensure_ascii=False)
        for v in cfg["variants"]:
            _parse(v)                                                   # 每個 variant 依其 format 可解析
        cfgs[tool] = {"variants": [f"{v['scope']}:{v['format']}" for v in cfg["variants"]],
                      "env": cfg["env"], "token_free": all(t not in text for t in op.tokens),
                      "url_port_ok": tool == "stdio" or f"{op.serve.url}/mcp/{GROUP}" in cfg["variants"][0]["content"]}
        assert cfg["env"] == ["SPILLWAY_TOKEN"] and "SPILLWAY_TOKEN" in cfg["variants"][0]["content"], cfg
        assert cfgs[tool]["token_free"] and cfgs[tool]["url_port_ok"], (tool, cfgs[tool])
    obs["mcp_config"] = cfgs

    # ---- 只聽 127.0.0.1;token 明文不外洩
    obs["listening"] = procs.listening(op.serve.proc.pid)
    assert obs["listening"] == [f"127.0.0.1:{op.serve.port}"]
    obs["token_leaks"] = op.assert_no_token_leak()
    record("auth_tools", obs)


# ---------------------------------------------------------------- 2. list_changed


async def _prove_get_stream(op: Op, inbox: list[Any]) -> int:
    """改一次 route 直到收到 list_changed(GET 串流已在伺服器端就緒);回傳嘗試次數。"""
    body = {"name": "review", "target": {"group": GROUP}, "mode": "task"}
    for i in range(50):
        op.ok(await asyncio.to_thread(op.http.put, "/api/routes/review", json=body))
        for _ in range(10):
            if inbox:
                return i + 1
            await asyncio.sleep(0.02)
    pytest.fail("GET 通知串流未就緒")


async def _drain(inbox: list[Any], quiet: float = 0.5, limit: float = 5.0) -> None:
    """等到 `quiet` 秒內沒有新通知(最多 `limit` 秒)再清空。"""
    deadline = time.monotonic() + limit
    seen = len(inbox)
    last = time.monotonic()
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        if len(inbox) != seen:
            seen, last = len(inbox), time.monotonic()
        elif time.monotonic() - last >= quiet:
            break
    else:
        pytest.fail("通知持續進來,無法清空")
    inbox.clear()


async def test_m3b_operation_list_changed(op: Op):
    inbox: list[Any] = []

    async def handler(message: Any) -> None:
        if is_list_changed(message):
            inbox.append(time.monotonic())

    obs: dict[str, Any] = {}
    async with op.mcp(message_handler=handler) as (s, _init, _sid):
        assert ask_route_enum((await s.list_tools()).tools) == ["review"]   # 決策 13:列過工具才登記
        obs["get_stream_ready_after_attempts"] = await _prove_get_stream(op, inbox)
        await _drain(inbox)               # 不讓 _prove_get_stream 的遲到通知被當成 review2 的通知
        t0 = time.monotonic()
        op.ok(await asyncio.to_thread(op.http.post, "/api/routes", json={
            "name": "review2", "target": {"group": GROUP}, "mode": "task"}), 201)

        async def wait() -> None:
            while not any(ts >= t0 for ts in inbox):
                await asyncio.sleep(0.02)

        await asyncio.wait_for(wait(), LIST_CHANGED_WAIT)
        obs["list_changed_latency_s"] = round(min(ts for ts in inbox if ts >= t0) - t0, 3)
        obs["notifications_after_create"] = len(inbox)
        obs["relisted_enum"] = ask_route_enum((await s.list_tools()).tools)
    assert obs["list_changed_latency_s"] <= LIST_CHANGED_WAIT
    assert obs["relisted_enum"] == ["review", "review2"]
    obs["token_leaks"] = op.assert_no_token_leak()
    record("list_changed", obs)


# ---------------------------------------------------------------- 3. stdio


async def test_m3b_operation_stdio(op: Op):
    bridge_home = op.tmp / "bridge-home"
    ok = await stdio_session(group=GROUP, port=op.serve.port, token=op.token, bridge_home=bridge_home,
                             fake_bin=op.fake.bin_dir, call=("ask", {"route": "review", "prompt": PROMPT}))
    assert ok.error is None and ok.init is not None, (ok.error, ok.returncode, redact_tokens(ok.stderr, TOKENS))
    assert ok.call is not None and ok.call.isError is False, ok.call
    denied = await stdio_session(group=GROUP, port=op.serve.port, token=op.token, depth="1",
                                 bridge_home=bridge_home, fake_bin=op.fake.bin_dir)
    obs = {
        "ok": {"list_changed_capability": ok.init.capabilities.tools.listChanged,
               "tools": [t.name for t in ok.tools], "ask_served_by": ok.call.structuredContent["served_by"],
               "returncode": ok.returncode},
        "depth_rejected": {"error_data": denied.error.data if denied.error else None,
                           "returncode": denied.returncode,
                           "stderr_has_code": "recursion_blocked" in denied.stderr},
        "bridge_created_data_dir": bridge_home.exists(),
        "stderr_token_free": all(t not in ok.stderr + denied.stderr for t in op.tokens),
    }
    assert obs["ok"]["tools"] == ["ask", "list_routes", "quota_status"]
    assert obs["ok"]["ask_served_by"] == CLAUDE and obs["ok"]["returncode"] == 0, obs
    assert denied.init is None and denied.error is not None and isinstance(denied.error.data, dict), \
        (denied.error, denied.returncode)
    assert denied.error.data["code"] == "recursion_blocked", denied.error
    assert obs["depth_rejected"]["returncode"] == 3 and obs["depth_rejected"]["stderr_has_code"], obs
    assert obs["bridge_created_data_dir"] is False and obs["stderr_token_free"]
    obs["token_leaks"] = op.assert_no_token_leak()
    record("stdio", obs)


# ---------------------------------------------------------------- 4. 停機


async def test_m3b_operation_shutdown(op: Op):
    obs: dict[str, Any] = {}
    serve = op.serve
    op.fake.set("claude", "hold_until_release")
    get_open = asyncio.Event()
    get_task = asyncio.ensure_future(raw_get_stream(f"{serve.url}/mcp/{GROUP}", op.token,
                                                    on_open=get_open.set, event_hooks=HttpLog.async_hooks()))
    await asyncio.wait_for(get_open.wait(), 15)
    async with op.mcp() as (s, _init, _sid):
        call = asyncio.ensure_future(s.call_tool("ask", {"route": "review", "prompt": PROMPT}))
        (rec,) = await asyncio.to_thread(op.fake.wait_for_records, "claude", 1, timeout=30)
        assert pid_alive(rec["pid"])
        t0 = time.monotonic()
        serve.proc.send_signal(signal.SIGTERM)
        get_closed = await asyncio.wait_for(get_task, GRACE + 10)
        result = await asyncio.wait_for(call, GRACE + 10)
        answered = time.monotonic() - t0
        await asyncio.to_thread(serve.proc.wait, GRACE + 15)
        exited = time.monotonic() - t0
    obs["sigterm"] = {
        "get_stream_closed_s": round(get_closed - t0, 3), "ask_answered_s": round(answered, 3),
        "serve_exit_s": round(exited, 3), "grace_s": GRACE, "returncode": serve.proc.returncode,
        "ask_is_error": result.isError,
        "ask_error_code": (result.structuredContent or {}).get("error", {}).get("code"),
        "leftover_fake_claude": pid_alive(rec["pid"]) or group_alive(rec["pid"]),
    }
    assert obs["sigterm"]["get_stream_closed_s"] < GET_CLOSE_LIMIT, obs           # GET 串流立即結束
    assert result.isError is True and obs["sigterm"]["ask_error_code"] == "shutting_down", result.content
    assert answered < GRACE and exited <= GRACE, obs                               # 寬限內回應並結束
    assert serve.proc.returncode == -signal.SIGTERM, serve.safe_log()
    assert obs["sigterm"]["leftover_fake_claude"] is False
    assert len(op.fake.records("claude")) == 1
    c = sqlite3.connect(str(op.home / "spillway.db"))
    try:
        last = c.execute("SELECT status, error FROM usage_ledger WHERE deployment_id = ? ORDER BY id DESC LIMIT 1",
                         (CLAUDE,)).fetchone()
    finally:
        c.close()
    obs["sigterm"]["ledger"] = list(last)
    assert tuple(last) == ("error", "shutdown")

    # ---- 另一次 serve:對 process group 送 SIGINT
    op.fake.set("claude", "ok")
    second = op.start_serve()
    t1 = time.monotonic()
    os.killpg(second.proc.pid, signal.SIGINT)
    await asyncio.to_thread(second.proc.wait, GRACE + 15)
    log = second.log_text()
    obs["sigint"] = {"returncode": second.proc.returncode, "elapsed_s": round(time.monotonic() - t1, 3),
                     "traceback_in_stderr": "Traceback" in log}
    assert second.proc.returncode == 130 and not obs["sigint"]["traceback_in_stderr"], second.safe_log()
    assert "Traceback" not in serve.log_text()
    obs["token_leaks"] = op.assert_no_token_leak()
    record("shutdown", obs)

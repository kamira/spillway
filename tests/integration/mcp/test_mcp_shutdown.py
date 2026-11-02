"""MCP 與 graceful shutdown(CHG-20261102-01 T3;決策 14、18;AM-6、AM-7)。

L1(`live_gateway`,行程內):
- lifespan 內 `app.state.shutdown_hooks` 有一個 MCP 掛鉤(lifespan 外為 T7 的預設空清單);`fire()` 只關每個 session 的 GET 通知串流
  (`sdk_compat.get_stream_open` 轉為 False),session 不被 terminate(之後的 POST 照常)。
- 停機中(`app.state.shutting_down`)新的 `/mcp` 請求(POST 與 GET 重連)回 503 `shutting_down`(spec 9.1.2)。
- 絆線:`iter_transports` 列出目前的 transport;transport 有公開的 `close_standalone_sse_stream()`。
- lifespan 關閉第 2 步:`wait_drain_tasks` 有上限地等待 `drain_tasks`,逾時再取消。
L2(子行程 `spillway serve`;假 claude;資料目錄為暫存 `SPILLWAY_HOME`;環境一律經 `safe_subprocess_env`):
- 有開啟中的 GET 串流(以收到 `tools/list_changed` 證明已就緒)時送 SIGTERM:3 秒內結束,同時進行中的
  `tools/call`(假 claude 延遲 2 秒)完整回應。
- 對照組「停用 auto-drain 且不關 GET」(以包裝程式把掛鉤本體換成 no-op):等滿寬限才結束。
- 寬限 2 秒、hold 住假 claude 的 MCP `ask` → `isError`、`structuredContent.error.code == "shutting_down"`、
  帳本 `error="shutdown"`、無殘留子行程。

停機中新的 `/mcp` 請求回 503 只在行程內驗證:子行程中 uvicorn 收到訊號後隨即關閉監聽 socket,新連線
到不了 app(同 `tests/integration/test_graceful_shutdown.py` 的說明)。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server.streamable_http import StreamableHTTPServerTransport

from spillway import cli, settings
from spillway.api.mcp import sdk_compat
from spillway.api.mcp import server as mcp_server
from spillway.app import wait_drain_tasks
from spillway.store import repo
from spillway.store.db import connect
from tests.fakes.cli_upstream import FakeCli, install, safe_subprocess_env
from tests.integration.mcp.conftest import MCP_ACCEPT
from tests.integration.test_graceful_shutdown import (
    CLAUDE_MEMBER,
    REAL_KEY_VARS,
    REPO_ROOT,
    ROUTE,
    SRC,
    _free_port,
    _group_alive,
    _pid_alive,
)

pytestmark = pytest.mark.anyio

skip_without_pgrep = pytest.mark.skipif(shutil.which("pgrep") is None, reason="需要 pgrep(macOS 驗收平台)")


# ---- L1:行程內 ----------------------------------------------------------------------------

def test_transport_has_public_close_standalone_sse_stream():
    """絆線:掛鉤只呼叫這個公開方法。"""
    t = StreamableHTTPServerTransport(mcp_session_id="x", is_json_response_enabled=False,
                                      event_store=None)
    assert callable(t.close_standalone_sse_stream)
    t.close_standalone_sse_stream()   # 沒有 GET 串流時為 no-op


def test_iter_transports_without_manager_is_empty():
    class _State:
        mcp_manager = None

    class _App:
        state = _State()

    assert sdk_compat.iter_transports(_App()) == []


def test_lifespan_registers_one_mcp_shutdown_hook(live_gateway):
    assert len(live_gateway.app.state.shutdown_hooks) == 1
    from spillway.app import create_app
    assert list(create_app().state.shutdown_hooks) == []   # lifespan 外維持預設空清單


async def test_hook_closes_only_get_stream_and_keeps_session(live_gateway):
    gw = live_gateway
    async with gw.mcp_session("all") as c1, gw.mcp_session("all") as c2:
        for c in (c1, c2):
            await gw.wait_stream_open(c.session_id)
        transports = gw.call_in_loop(sdk_compat.iter_transports, gw.app)
        assert {t.mcp_session_id for t in transports} >= {c1.session_id, c2.session_id}

        gw.call_in_loop(gw.app.state.shutdown_hooks.fire)
        for c in (c1, c2):
            for _ in range(100):
                if not gw.stream_open(c.session_id):
                    break
                await asyncio.sleep(0.02)
            assert gw.stream_open(c.session_id) is False
        # 不 terminate session:同一個 session 的 POST 照常
        tools = (await c1.session.list_tools()).tools
        assert [t.name for t in tools] == ["ask", "list_routes", "quota_status"]
        result = await c2.session.call_tool("list_routes", {})
        assert result.isError is False


def _assert_shutting_down(resp: httpx.Response) -> None:
    assert resp.status_code == 503, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["error"]["code"] == "shutting_down"


async def test_new_mcp_requests_while_shutting_down_are_503(live_gateway):
    gw = live_gateway
    async with gw.mcp_session("all") as c:
        await gw.wait_stream_open(c.session_id)
        gw.app.state.shutting_down = True
        gw.call_in_loop(gw.app.state.shutdown_hooks.fire)
        hdrs = {**gw.auth_headers(), "Mcp-Session-Id": c.session_id, "MCP-Protocol-Version": "2025-06-18"}
        async with gw.async_http() as http:
            get = await http.get("/mcp/all", headers={**hdrs, "Accept": "text/event-stream"})
            _assert_shutting_down(get)                         # GET 重連
            post = await http.post("/mcp/all", headers={**hdrs, "Accept": MCP_ACCEPT},
                                   json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
            _assert_shutting_down(post)
        gw.app.state.shutting_down = False                     # 讓用戶端離開時的 DELETE 正常結束


async def test_wait_drain_tasks_is_bounded_and_cancels_leftovers():
    class _State:
        drain_tasks: set[asyncio.Task[Any]] = set()

    class _App:
        state = _State()

    app = _App()
    quick = asyncio.ensure_future(asyncio.sleep(0.05))
    stuck = asyncio.ensure_future(asyncio.sleep(3600))
    app.state.drain_tasks = {quick, stuck}
    start = time.monotonic()
    await wait_drain_tasks(app, timeout=0.3)   # type: ignore[arg-type]
    assert time.monotonic() - start < 1.5
    assert quick.done() and not quick.cancelled()
    assert stuck.cancelled()
    await wait_drain_tasks(app, timeout=0.3)   # type: ignore[arg-type]  # 都已結束 → 立即返回


# ---- L2:子行程 serve ----------------------------------------------------------------------

GRACE = 5.0         # 分得出「只關 GET 立即結束」與「等滿寬限」
FAST_LIMIT = 3.0    # 只關 GET:SIGTERM 後 3 秒內結束
TOOL_DELAY = 2.0    # 進行中 tools/call 的假 claude 延遲

# 對照組:把掛鉤本體換成 no-op(仍停用 sse-starlette 的 auto-drain,GET 串流不被關)
_NO_HOOK = ("import sys; import spillway.api.mcp.server as m; m.close_standalone_streams = lambda app: 0; "
            "from spillway.cli import main; raise SystemExit(main(sys.argv[1:]))")


class McpServe:
    def __init__(self, home: Path, graceful: float, *, close_get: bool = True) -> None:
        home.mkdir()
        self.home = home
        self.fake = FakeCli(home, install(home / "fake-cli-bin"))
        conn = connect(home / "spillway.db")   # migration + token(serve 看到已有雜湊就不印)
        try:
            self.token = settings.rotate_access_token(conn)
        finally:
            conn.close()
        base = {k: v for k, v in os.environ.items() if k not in REAL_KEY_VARS}
        base.update(SPILLWAY_HOME=str(home), PYTHONPATH=str(SRC), LITELLM_LOCAL_MODEL_COST_MAP="True")
        base[cli.TEST_GRACEFUL_TIMEOUT_ENV] = str(graceful)
        env = safe_subprocess_env(base, fake_bin=self.fake.bin_dir)
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        head = [sys.executable, "-m", "spillway"] if close_get else [sys.executable, "-c", _NO_HOOK]
        self.proc = subprocess.Popen(
            [*head, "serve", "--port", str(self.port)], env=env, cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.out = self.err = ""

    def wait_ready(self) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self.out, self.err = self.proc.communicate()
                pytest.fail(f"serve 提前結束 rc={self.proc.returncode}\n{self.err}")
            try:
                if httpx.get(f"{self.url}/api/settings/context", timeout=2, trust_env=False).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.2)
        else:
            pytest.fail("serve 未在 60 秒內就緒")
        for path, body in (("/api/deployments", CLAUDE_MEMBER), ("/api/routes", ROUTE)):
            r = httpx.post(self.url + path, json=body, timeout=30, trust_env=False)
            assert r.status_code == 201, r.text

    def wait_exit(self, limit: float) -> float:
        start = time.monotonic()
        try:
            self.out, self.err = self.proc.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            pytest.fail(f"serve 未在 {limit} 秒內結束")
        return time.monotonic() - start

    def stop(self) -> None:
        if self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.communicate()
        for rec in self.fake.records("claude"):
            if _group_alive(rec["pid"]):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(rec["pid"], signal.SIGKILL)
        shutil.rmtree(self.fake.control_dir, ignore_errors=True)

    def ledger(self) -> list[repo.LedgerRow]:
        conn = connect(self.home / "spillway.db")
        try:
            return repo.list_ledger(conn)
        finally:
            conn.close()

    @contextlib.asynccontextmanager
    async def session(self, inbox: list[Any]) -> AsyncIterator[ClientSession]:
        async def handler(message: Any) -> None:
            if isinstance(message, types.ServerNotification) and isinstance(
                    message.root, types.ToolListChangedNotification):
                inbox.append(message)

        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {self.token}"}, trust_env=False,
                                     timeout=httpx.Timeout(30.0, read=60.0)) as client:
            # 伺服器結束後用戶端的收尾(DELETE、GET 重連)會失敗;那不是本測試的斷言對象——只吞收尾的例外,
            # 測試本體的例外照常往上丟
            body_failed = False
            try:
                async with streamable_http_client(f"{self.url}/mcp/all", http_client=client) as (r, w, _):
                    async with ClientSession(r, w, message_handler=handler) as s:
                        await s.initialize()
                        try:
                            yield s
                        except BaseException:
                            body_failed = True
                            raise
            except Exception:
                if body_failed:
                    raise

    async def open_get_stream(self, s: ClientSession, inbox: list[Any]) -> None:
        """登記 session 並證明 GET 通知串流已在伺服器端就緒:改一次 route,等到收到 list_changed。"""
        await s.list_tools()
        async with httpx.AsyncClient(base_url=self.url, trust_env=False, timeout=30) as http:
            for i in range(50):   # 用戶端的 GET 可能還沒連上:每次改動都會送一次通知
                r = await http.put(f"/api/routes/{ROUTE['name']}", json={**ROUTE, "tags": [f"x{i}"]})
                assert r.status_code == 200, r.text
                for _ in range(10):
                    if inbox:
                        return
                    await asyncio.sleep(0.02)
        pytest.fail("GET 通知串流未就緒")


@pytest.fixture
def mcp_serve(tmp_path):
    started: list[McpServe] = []

    def _start(graceful: float, *, close_get: bool = True) -> McpServe:
        s = McpServe(tmp_path / f"home{len(started)}", graceful, close_get=close_get)
        started.append(s)
        s.wait_ready()
        return s

    yield _start
    for s in started:
        s.stop()


async def _wait_fake_started(s: McpServe) -> dict[str, Any]:
    (rec,) = await asyncio.to_thread(s.fake.wait_for_records, "claude", 1, timeout=30)
    assert _pid_alive(rec["pid"])
    return rec


def _ask(s: ClientSession) -> asyncio.Task[types.CallToolResult]:
    return asyncio.ensure_future(s.call_tool("ask", {"route": ROUTE["name"], "prompt": "do it"}))


@skip_without_pgrep
async def test_sigterm_with_open_get_stream_exits_fast_and_inflight_call_completes(mcp_serve):
    s = mcp_serve(GRACE)
    s.fake.set("claude", "ok", delay_seconds=TOOL_DELAY)
    inbox: list[Any] = []
    async with s.session(inbox) as session:
        await s.open_get_stream(session, inbox)
        call = _ask(session)
        rec = await _wait_fake_started(s)
        signalled = time.monotonic()
        s.proc.send_signal(signal.SIGTERM)
        result = await asyncio.wait_for(call, GRACE + 5)
        await asyncio.to_thread(s.wait_exit, GRACE + 10)
        elapsed = time.monotonic() - signalled
    assert elapsed < FAST_LIMIT, f"開啟中的 GET 串流拖住停機({elapsed:.1f}s)"
    assert s.proc.returncode == -signal.SIGTERM, s.err
    assert result.isError is False, result.content           # 進行中的 tools/call 完整回應
    assert result.structuredContent["served_by"] == "claude-sub"
    assert result.structuredContent["text"]
    assert not _group_alive(rec["pid"])
    rows = [r for r in s.ledger() if r.deployment_id == "claude-sub"]
    assert len(rows) == 1 and rows[0].status == "ok", rows


@skip_without_pgrep
async def test_control_get_stream_not_closed_waits_full_grace(mcp_serve):
    s = mcp_serve(GRACE, close_get=False)
    inbox: list[Any] = []
    async with s.session(inbox) as session:
        await s.open_get_stream(session, inbox)
        signalled = time.monotonic()
        s.proc.send_signal(signal.SIGTERM)
        await asyncio.to_thread(s.wait_exit, GRACE + 10)
        elapsed = time.monotonic() - signalled
    assert elapsed >= GRACE - 0.5, f"沒有等滿寬限({elapsed:.1f}s);對照組失效"
    assert s.proc.returncode == -signal.SIGTERM, s.err


@skip_without_pgrep
async def test_drain_expired_mcp_ask_returns_shutting_down_tool_error(mcp_serve):
    grace = 2.0
    s = mcp_serve(grace)
    s.fake.set("claude", "hold_until_release")
    inbox: list[Any] = []
    async with s.session(inbox) as session:
        await s.open_get_stream(session, inbox)
        call = _ask(session)
        rec = await _wait_fake_started(s)
        signalled = time.monotonic()
        s.proc.send_signal(signal.SIGTERM)
        result = await asyncio.wait_for(call, grace + 10)
        answered = time.monotonic() - signalled
        await asyncio.to_thread(s.wait_exit, grace + 15)
    assert answered < grace, f"沒有在寬限內回應({answered:.1f}s)"
    assert result.isError is True, result.content             # 工具錯誤,不是 JSON-RPC error
    assert result.structuredContent["error"]["code"] == "shutting_down", result.structuredContent
    assert s.proc.returncode == -signal.SIGTERM, s.err
    assert not _group_alive(rec["pid"]), "假 claude 或其孫行程仍在"
    assert not _pid_alive(rec["pid"])
    assert len(s.fake.records("claude")) == 1
    rows = [r for r in s.ledger() if r.deployment_id == "claude-sub"]
    assert len(rows) == 1, rows
    assert rows[0].status == "error" and rows[0].error == "shutdown", rows[0]

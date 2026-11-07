"""MCP 用戶端與 `mcp-stdio` 子行程的共用驅動(CHG-20261107-03 T2;決策 8 ⑦;AM-15)。

由 `tests/features/support/mcp_proc.py` 複製改寫成黑箱版(原檔不動,由 T4 改用本模組):
子行程 serve 一律用 `tests.blackbox.harness.Serve`;`mcp-stdio` 經 `SPILLWAY_CMD` 執行;
GET 通知串流是否就緒經 `GET /__test__/mcp/sessions/{id}`(`Serve.wait_stream_open`)。不 import spillway。

- `LoopThread`:背景 thread 的事件迴圈;同步程式(behave 步驟)以 `run(coro, timeout)` 送入協程。
  SDK 用戶端的 anyio cancel scope 必須在同一個 task 進出,所以「跨步驟持有的 session」一律由
  `HeldSession` 在迴圈上的單一 task 內進出。
- `mcp_http_session(url, token, ...)`:官方 SDK `streamable_http_client` + 自建 `httpx.AsyncClient`
  (`trust_env=False`,系統代理收不到 Bearer);`event_hooks` 原樣交給 httpx。yield
  `(session, init, session_id)`。伺服器先結束時,只吞「離開 context 時的收尾例外」。
- `McpClient(session, init, session_id)`:`Serve.mcp_session` yield 的物件(同 `tests/helpers/live_gateway`)。
- `HeldSession`:跨步驟持有一個 session;`inbox` 收 `notifications/tools/list_changed`。
- `raw_get_stream(url, token, *, on_open, event_hooks=None)`:原生 httpx 建 session 後開 GET 通知串流並讀到
  結束,回傳結束時的 `time.monotonic()`。
- `stdio_session(*, group, port, token, depth=None, call=None, bridge_home, fake_bin=None, timeout=60)`:
  SDK 的 stdio 用戶端經 `SPILLWAY_CMD mcp-stdio --group <g> --port <p>` 送出 `initialize`(成功時再
  `tools/list`,可再呼叫一個工具),回傳 `StdioOutcome`;bridge 的結束碼由一層透明的包裝行程寫入暫存檔
  (先寫 `.tmp` 再 `os.replace`)取得。
- `is_list_changed(message)`、`ask_route_enum(tools)`、`pid_alive(pid)`、`group_alive(pgid)`、
  `redact(text, secrets)`。

token 明文只存在呼叫端記憶體與子行程環境變數,不寫入任何檔案,也不出現在斷言訊息。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession, McpError, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from tests.blackbox.harness import child_env, spillway_cmd
from tests.blackbox.mask import redact

__all__ = ["INIT_BODY", "MCP_ACCEPT", "HeldSession", "LoopThread", "McpClient", "StdioOutcome", "ask_route_enum",
           "group_alive", "is_list_changed", "mcp_http_session", "pid_alive", "raw_get_stream", "redact",
           "stdio_session"]

MCP_ACCEPT = "application/json, text/event-stream"
INIT_BODY = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "spillway-blackbox", "version": "0"}}}

# bridge 的透明包裝:stdin/stdout/stderr 直接繼承;結束後把 bridge 的結束碼寫入 argv[1]
_RC_WRAPPER = ("import os, subprocess, sys\n"
               "rc = subprocess.call(sys.argv[2:])\n"
               "tmp = sys.argv[1] + '.tmp'\n"
               "with open(tmp, 'w') as f:\n"
               "    f.write(str(rc))\n"
               "os.replace(tmp, sys.argv[1])\n"
               "sys.exit(rc)\n")


def is_list_changed(message: Any) -> bool:
    return isinstance(message, types.ServerNotification) and isinstance(
        message.root, types.ToolListChangedNotification)


def ask_route_enum(tools: list[types.Tool]) -> list[str]:
    ask = next(t for t in tools if t.name == "ask")
    return list(ask.inputSchema["properties"]["route"].get("enum") or [])


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")


def group_alive(pgid: int) -> bool:
    r = subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


# ---------------------------------------------------------------------------- 事件迴圈


class LoopThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, name="blackbox-mcp-loop", daemon=True)
        self._thread.start()

    def run(self, coro: Awaitable[Any], timeout: float = 60.0) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)  # type: ignore[arg-type]

    def submit(self, coro: Awaitable[Any]) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)  # type: ignore[arg-type]

    def close(self) -> None:
        if self.loop.is_closed():
            return

        async def _cancel_rest() -> None:
            rest = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in rest:
                t.cancel()
            await asyncio.gather(*rest, return_exceptions=True)

        with contextlib.suppress(Exception):
            self.run(_cancel_rest(), timeout=10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(10)
        if not self._thread.is_alive():
            self.loop.close()


# ---------------------------------------------------------------------------- Streamable HTTP


@dataclass
class McpClient:
    session: ClientSession
    init: types.InitializeResult
    session_id: str | None


@asynccontextmanager
async def mcp_http_session(
    url: str,
    token: str | None,
    *,
    headers: Mapping[str, str] | None = None,
    message_handler: Callable[[Any], Awaitable[None]] | None = None,
    event_hooks: Mapping[str, list[Any]] | None = None,
    read_timeout: float = 60.0,
) -> AsyncIterator[tuple[ClientSession, types.InitializeResult, str | None]]:
    hdrs: dict[str, str] = {}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    hdrs.update(headers or {})
    body_failed = False
    try:
        async with httpx.AsyncClient(headers=hdrs, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(30.0, read=read_timeout),
                                     event_hooks=dict(event_hooks or {})) as client:
            async with streamable_http_client(url, http_client=client) as (r, w, sid):
                kw: dict[str, Any] = {}
                if message_handler is not None:
                    kw["message_handler"] = message_handler
                async with ClientSession(r, w, **kw) as session:
                    init = await session.initialize()
                    try:
                        yield session, init, sid()
                    except BaseException:
                        body_failed = True
                        raise
    except Exception:
        # 伺服器先結束(停機情境)時,用戶端離開 context 的 DELETE / GET 重連會失敗;那不是斷言對象
        if body_failed:
            raise


@dataclass
class HeldSession:
    """跨 behave 步驟持有的 SDK session(在 `LoopThread` 上的單一 task 內進出)。"""

    loop: LoopThread
    url: str
    token: str | None
    inbox: list[Any] = field(default_factory=list)
    session: ClientSession | None = None
    init: types.InitializeResult | None = None
    session_id: str | None = None
    _stop: asyncio.Event | None = None
    _task: Any = None

    def open(self, timeout: float = 30.0) -> HeldSession:
        ready = threading.Event()
        box: dict[str, BaseException] = {}

        async def handler(message: Any) -> None:
            if is_list_changed(message):
                self.inbox.append(message)

        async def hold() -> None:
            self._stop = asyncio.Event()
            try:
                async with mcp_http_session(self.url, self.token, message_handler=handler) as (s, init, sid):
                    self.session, self.init, self.session_id = s, init, sid
                    ready.set()
                    await self._stop.wait()
            except BaseException as exc:  # noqa: BLE001 — 交給 open() 回報
                box["exc"] = exc
                raise
            finally:
                ready.set()

        self._task = self.loop.submit(hold())
        assert ready.wait(timeout), "MCP session 未在時限內建立"
        if "exc" in box:
            raise box["exc"]
        return self

    def call(self, fn: Callable[[ClientSession], Awaitable[Any]], timeout: float = 60.0) -> Any:
        assert self.session is not None
        return self.loop.run(fn(self.session), timeout)

    def close(self, timeout: float = 15.0) -> None:
        if self._stop is not None:
            self.loop.loop.call_soon_threadsafe(self._stop.set)
        if self._task is not None:
            with contextlib.suppress(Exception):
                self._task.result(timeout)
            self._task = None


async def raw_get_stream(url: str, token: str, *, on_open: Callable[[], None],
                         event_hooks: Mapping[str, list[Any]] | None = None) -> float:
    """以原生 httpx 建立一個 MCP session(initialize + initialized),再開 GET 通知串流並讀到結束;
    回傳串流結束時的 `time.monotonic()`。`on_open` 在 GET 回 200 標頭後呼叫(串流已在伺服器端登記)。"""
    hdrs = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(headers=hdrs, trust_env=False, timeout=httpx.Timeout(30.0, read=120.0),
                                 event_hooks=dict(event_hooks or {})) as http:
        r = await http.post(url, json=INIT_BODY, headers={"Accept": MCP_ACCEPT})
        assert r.status_code == 200, r.text
        sid = r.headers["mcp-session-id"]
        proto = {"Mcp-Session-Id": sid, "MCP-Protocol-Version": INIT_BODY["params"]["protocolVersion"]}
        r = await http.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                            headers={**proto, "Accept": MCP_ACCEPT})
        assert r.status_code == 202, r.text
        async with http.stream("GET", url, headers={**proto, "Accept": "text/event-stream"}) as resp:
            assert resp.status_code == 200, resp.status_code
            on_open()
            with contextlib.suppress(httpx.HTTPError):
                async for _ in resp.aiter_bytes():
                    pass
        return time.monotonic()


# ---------------------------------------------------------------------------- stdio


@dataclass
class StdioOutcome:
    error: types.ErrorData | None
    init: types.InitializeResult | None
    tools: list[types.Tool]
    call: types.CallToolResult | None
    returncode: int | None
    stderr: str = field(repr=False)      # 不進 repr:斷言訊息不帶 stderr 原文


async def stdio_session(
    *,
    group: str,
    port: int,
    token: str | None,
    depth: str | None = None,
    call: tuple[str, dict[str, Any]] | None = None,
    bridge_home: Path,
    fake_bin: Path | None = None,
    timeout: float = 60.0,
) -> StdioOutcome:
    """SDK 的 stdio 用戶端 → 子行程 `SPILLWAY_CMD mcp-stdio`:initialize;成功時 `tools/list` 並(選擇性)
    呼叫一個工具。"""
    with tempfile.TemporaryDirectory(prefix="bb-stdio-") as tmp:
        rc_file = Path(tmp) / "rc"
        err_path = Path(tmp) / "stderr"
        extra: dict[str, str] = {}
        if token is not None:
            extra["SPILLWAY_TOKEN"] = token
        if depth is not None:
            extra["SPILLWAY_DEPTH"] = depth
        env = child_env(bridge_home, fake_bin=fake_bin, extra=extra)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", _RC_WRAPPER, str(rc_file), *spillway_cmd(), "mcp-stdio", "--group", group,
                  "--port", str(port)],
            env=env, cwd=tmp)
        error = init = result = None
        tools: list[types.Tool] = []
        with open(err_path, "w", encoding="utf-8") as errlog:
            async def body() -> None:
                nonlocal error, init, result, tools
                async with stdio_client(params, errlog=errlog) as (r, w):
                    async with ClientSession(r, w) as s:
                        try:
                            init = await s.initialize()
                        except McpError as exc:
                            error = exc.error
                            return
                        tools = (await s.list_tools()).tools
                        if call is not None:
                            result = await s.call_tool(call[0], call[1])

            await asyncio.wait_for(body(), timeout)
        deadline = time.monotonic() + 15
        while not rc_file.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        rc = int(rc_file.read_text()) if rc_file.exists() and rc_file.read_text() else None
        return StdioOutcome(error=error, init=init, tools=tools, call=result, returncode=rc,
                            stderr=err_path.read_text(encoding="utf-8", errors="replace"))

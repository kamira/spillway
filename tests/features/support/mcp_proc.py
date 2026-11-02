"""MCP 用戶端與子行程的共用驅動(CHG-20261102-01 T8;m3b_mcp.feature 與 tests/acceptance/test_m3b_operation.py)。

為什麼另寫一份:`tests/helpers/live_gateway.py` 的 `mcp_session` 綁在行程內的 `LiveGateway`;子行程 serve
(停機、stdio 情境需要真實訊號)與 behave 的同步步驟需要一個不依附 `LiveGateway` 的版本。

- `LoopThread`:背景 thread 的事件迴圈;同步程式(behave 步驟)以 `run(coro, timeout)` 送入協程。
  SDK 用戶端的 anyio cancel scope 必須在同一個 task 進出,所以「跨步驟持有的 session」一律由
  `HeldSession` 在迴圈上的單一 task 內進出(AM-4 的同一個問題)。
- `mcp_http_session(url, token, ...)`:官方 SDK `streamable_http_client` + 自建 `httpx.AsyncClient`
  (`trust_env=False`,系統代理收不到 Bearer);`event_hooks` 原樣交給 httpx(驗收的 `http-log.jsonl`)。
  yield `(session, init, session_id)`。伺服器先結束時,只吞「離開 context 時的收尾例外」。
- `HeldSession`:跨步驟持有一個 session;`inbox` 收 `notifications/tools/list_changed`。
- `ServeProc`:子行程 `python -m spillway serve --port <隨機>`(環境經 `safe_subprocess_env`;
  `SPILLWAY_TEST_GRACEFUL_TIMEOUT`;`start_new_session=True`;stdout/stderr 寫入檔案)。
- `stdio_session(...)`:官方 SDK `stdio_client` 經子行程 `python -m spillway mcp-stdio` 送出
  `initialize`(成功時再 `tools/list`,可再呼叫一個工具),回傳 `StdioOutcome`;bridge 的結束碼由一層透明的
  包裝行程寫入暫存檔(先寫 `.tmp` 再 `os.replace`)取得(SDK 的 stdio 用戶端不提供結束碼;包裝行程的
  stdin/stdout 直接繼承給 bridge,不轉送、不解析)。
- `is_list_changed(message)`:用戶端 `message_handler` 收到的訊息是否為 `notifications/tools/list_changed`。
- `redact(text, secrets)`:斷言訊息與錯誤中顯示日誌/stderr 前把 token 換成 `<token>`。

token 明文只存在呼叫端記憶體與子行程環境變數,不寫入任何檔案,也不出現在斷言訊息。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
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

from tests.acceptance.procs import free_port, listening
from tests.fakes.cli_upstream import safe_subprocess_env

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src"
REAL_KEY_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
GRACEFUL_ENV = "SPILLWAY_TEST_GRACEFUL_TIMEOUT"
MCP_ACCEPT = "application/json, text/event-stream"
INIT_BODY = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "spillway-m3b", "version": "0"}}}

# bridge 的透明包裝:stdin/stdout/stderr 直接繼承;結束後把 bridge 的結束碼寫入 argv[1](先寫暫存檔再
# os.replace,讀取端不會讀到寫一半的內容)
_RC_WRAPPER = ("import os, subprocess, sys\n"
               "rc = subprocess.call(sys.argv[2:])\n"
               "tmp = sys.argv[1] + '.tmp'\n"
               "with open(tmp, 'w') as f:\n"
               "    f.write(str(rc))\n"
               "os.replace(tmp, sys.argv[1])\n"
               "sys.exit(rc)\n")


def redact(text: str, secrets: Any) -> str:
    for s in secrets or ():
        if s:
            text = text.replace(s, "<token>")
    return text


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
        self._thread = threading.Thread(target=self.loop.run_forever, name="m3b-loop", daemon=True)
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


# ---------------------------------------------------------------------------- 子行程 serve


def child_env(home: Path, *, fake_bin: Path | None, graceful: float | None = None,
              extra: Mapping[str, str] | None = None) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items()
            if k not in REAL_KEY_VARS and k not in ("SPILLWAY_TOKEN", "SPILLWAY_DEPTH")
            and not k.lower().endswith("_proxy")}
    base["SPILLWAY_HOME"] = str(home)
    base["PYTHONPATH"] = os.pathsep.join([str(SRC), str(REPO_ROOT)])
    base["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    if graceful is not None:
        base[GRACEFUL_ENV] = str(graceful)
    base.update(extra or {})
    return safe_subprocess_env(base, fake_bin=fake_bin)


class ServeProc:
    """`python -m spillway serve --port <port>`(資料目錄由呼叫端準備;token 已產生時 serve 不印)。"""

    def __init__(self, home: Path, *, fake_bin: Path | None, graceful: float,
                 log_path: Path, extra_env: Mapping[str, str] | None = None,
                 secrets: Any = ()) -> None:
        self.home = home
        self.secrets = secrets          # 錯誤訊息顯示日誌前遮掉(呼叫端持有的 token 清單)
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.log_path = log_path
        self.env = child_env(home, fake_bin=fake_bin, graceful=graceful, extra=extra_env)
        self._log = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 — stop() 關閉
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "spillway", "serve", "--port", str(self.port)], env=self.env,
            cwd=str(REPO_ROOT), stdout=self._log, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)

    def wait_ready(self, timeout: float = 60.0) -> None:
        """行程仍在、本埠的監聽者是這個 serve 行程(lsof),且 HTTP 回 200。"""
        deadline = time.monotonic() + timeout
        want = f"127.0.0.1:{self.port}"
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"serve 提前結束 rc={self.proc.returncode}\n{self.safe_log()}")
            if want not in listening(self.proc.pid):
                time.sleep(0.2)
                continue
            try:
                r = httpx.get(f"{self.url}/api/settings/context", timeout=2, trust_env=False)
                if r.status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.2)
        raise RuntimeError("serve 未在時限內就緒")

    def wait_exit(self, timeout: float) -> float:
        start = time.monotonic()
        self.proc.wait(timeout)
        return time.monotonic() - start

    def log_text(self) -> str:
        self._log.flush()
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def safe_log(self) -> str:
        return redact(self.log_text(), self.secrets)

    def stop(self) -> None:
        if self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                self.proc.wait(15)
        with contextlib.suppress(Exception):
            self._log.close()


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
    """SDK 的 stdio 用戶端 → 子行程 `mcp-stdio`:initialize;成功時 `tools/list` 並(選擇性)呼叫一個工具。"""
    with tempfile.TemporaryDirectory(prefix="m3b-stdio-") as tmp:
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
            args=["-c", _RC_WRAPPER, str(rc_file), sys.executable, "-m", "spillway", "mcp-stdio",
                  "--group", group, "--port", str(port)],
            env=env, cwd=str(REPO_ROOT))
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

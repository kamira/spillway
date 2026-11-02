"""`spillway mcp-stdio` 的 stdio ↔ Streamable HTTP 轉送(CHG-20261102-01 T4;決策 15,決策審議修訂 AM-1)。

給不支援 HTTP 的工具用:stdin/stdout 為換行分隔的 JSON-RPC,轉送到
`http://127.0.0.1:<port>/mcp/<group>`;不解析工具內容(工具邏輯只在閘道內實作一次)。

- 傳輸:`mcp.client.streamable_http.streamable_http_client(url, http_client=client)`,`client` 為自建的
  `httpx.AsyncClient(headers=..., trust_env=False, timeout=httpx.Timeout(30, read=660),
  follow_redirects=False)`(SDK 仍會自行跟隨同 origin 的 redirect)——`trust_env=False` 讓系統代理(`HTTP_PROXY`/`ALL_PROXY`、macOS 系統代理)
  收不到明文 Bearer;read 660 秒涵蓋 600 秒的 `ask`。URL 主機固定 `127.0.0.1`(不提供 `--host`/`--url`),
  群組名以 `quote(g, safe="")` 編碼。
- stdio 端:`mcp.server.stdio.stdio_server(stdin=, stdout=)`,stdin/stdout 傳入本模組的轉接物件:
  stdin 由 daemon thread 以 `os.read(0)` 讀取(可被取消——SDK 預設的 `anyio.wrap_file` 讀取在 thread 中
  不可取消,被閘道拒絕時要等到 stdin 下一行才結束);stdout 以 `os.write(1)` 寫出(錯誤回應在 SDK 的
  stdio context 結束之後仍可寫出)。stdout 只寫 JSON-RPC。
- 兩個 read stream 吐出 `Exception` 物件(解析失敗等)時,只在 stderr 記類型,不轉送。
- 錯誤對應層:記錄從 stdin 進來、尚未回應的 request id。閘道對 POST 回 401/403/404/503 時(回應 hook 記下
  狀態碼並丟 `httpx.HTTPStatusError`——SDK 對 POST 的 404 不丟例外、只回「Session terminated」,所以由
  hook 統一丟出),以 `except* httpx.HTTPStatusError` 捕捉並依狀態碼對應:401 `unauthorized`、
  403 `recursion_blocked`(bridge 固定 Host 且不送 Origin,403 只可能是這個)、404 `not_found`、
  503 `shutting_down`;對每個 pending id 寫出
  `{"jsonrpc":"2.0","id":<id>,"error":{"code":-32000,"message":"<code>: rejected by gateway (HTTP <s>)",
  "data":{"code":"<code>","http_status":<s>}}}`,stderr 印一行固定字串,結束碼 3。其他 HTTP 狀態碼、
  `httpx.ConnectError`(閘道未啟動)與其他傳輸錯誤 → 對 pending id 寫出錯誤(不帶 `data.code`),結束碼 1。
- token:`SPILLWAY_TOKEN` 先 `strip()`;未設定、空值或含 CR/LF → 結束碼 2,不連線。
- depth:`SPILLWAY_DEPTH` 為空或未設定時不送 header;有值時原樣放進 `X-Spillway-Depth`(spec 8.1.6);
  含 CR/LF → 結束碼 2。
- 不開資料庫、不取 `spillway.lock`、不建立資料目錄。
- 日誌:`mcp`、`httpx`、`httpcore` logger 設為 ERROR、不往上傳遞、只掛 `NullHandler`(stderr 不出現 SDK 的
  traceback);自己的錯誤只輸出固定字串,不帶例外內容;不記 token、prompt 與回應。
- `KeyboardInterrupt`(Ctrl-C)→ 結束碼 130,不印 traceback。stdin EOF → 等進行中的 request 至多
  `EOF_DRAIN_TIMEOUT` 秒後結束,結束碼 0;逾時仍未回應的 id 寫出錯誤,結束碼 1。
- 閘道端串流自行結束(例如通知 POST 被拒絕,SDK 吞掉例外並關閉 read stream)→ 同時停止讀 stdin、立即結束:
  記到拒絕狀態碼時結束碼 3(錯誤帶 `data.code`),否則 1;未回應的 id(含 stdin 已送出、尚未轉送者)都寫出錯誤。

`run(group, port, environ=None) -> int` 為 `cli.py` 的入口。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from spillway.settings import DEFAULT_HOST, EXIT_SIGINT, mcp_url

from .sdk_logging import install_sdk_root_log_filter

__all__ = [
    "EXIT_OK", "EXIT_ERROR", "EXIT_CONFIG", "EXIT_REJECTED", "EXIT_SIGINT", "REJECT_CODES",
    "TOKEN_ENV", "DEPTH_ENV", "DEPTH_HEADER", "HOST", "bridge_url", "build_headers", "run",
]

EXIT_OK = 0
EXIT_ERROR = 1        # 閘道未啟動或其他傳輸錯誤
EXIT_CONFIG = 2       # SPILLWAY_TOKEN / SPILLWAY_DEPTH 不合法(未連線)
EXIT_REJECTED = 3     # 被閘道拒絕(401/403/404/503)

TOKEN_ENV = "SPILLWAY_TOKEN"
DEPTH_ENV = "SPILLWAY_DEPTH"
DEPTH_HEADER = "X-Spillway-Depth"
HOST = DEFAULT_HOST
REJECT_CODES: dict[int, str] = {
    401: "unauthorized",
    403: "recursion_blocked",
    404: "not_found",
    503: "shutting_down",
}
JSONRPC_SERVER_ERROR = -32000
EOF_DRAIN_TIMEOUT = 5.0     # stdin EOF 後等待進行中 request 回應的上限(秒);逾時對其餘 id 寫出錯誤
STDIN_DRAIN_TIMEOUT = 1.0   # 閘道端結束後,收集 stdin 已送出、尚未轉送的 request id 的上限(秒)
_PREFIX = "spillway mcp-stdio"


class ConfigError(Exception):
    """環境變數不合法;訊息為固定字串(不含 token)。"""


def _say(message: str) -> None:
    try:
        print(f"{_PREFIX}: {message}", file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def bridge_url(group: str, port: int) -> str:
    return mcp_url(port, group)


def build_headers(environ: Mapping[str, str]) -> dict[str, str]:
    """由環境變數組出 HTTP headers;不合法時丟 `ConfigError`(訊息不含 token 值)。"""
    token = (environ.get(TOKEN_ENV) or "").strip()
    if not token:
        raise ConfigError(f"{TOKEN_ENV} 未設定或為空;未連線")
    if "\r" in token or "\n" in token:
        raise ConfigError(f"{TOKEN_ENV} 含換行字元(CR/LF);未連線")
    headers = {"Authorization": f"Bearer {token}"}
    depth = environ.get(DEPTH_ENV)
    if depth:
        if "\r" in depth or "\n" in depth:
            raise ConfigError(f"{DEPTH_ENV} 含換行字元(CR/LF);未連線")
        headers[DEPTH_HEADER] = depth
    return headers


# ---------------------------------------------------------------------------- stdio 轉接物件

class _StdinLines:
    """`async for line in stdin`:daemon thread 以 `os.read` 讀 fd,逐行交回事件迴圈(可取消)。

    用 `os.read` 而非 `sys.stdin.buffer`:daemon thread 停在 BufferedReader 內時,直譯器結束會卡在它的鎖。"""

    def __init__(self, fd: int = 0) -> None:
        self._fd = fd
        self._closed = False
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def put(item: str | None) -> None:
            try:
                loop.call_soon_threadsafe(self._queue.put_nowait, item)
            except RuntimeError:   # 迴圈已關閉
                pass

        def reader() -> None:
            buf = b""
            try:
                while chunk := os.read(self._fd, 65536):
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for raw in lines:
                        if raw.strip():
                            put(raw.decode("utf-8", errors="replace"))
            except OSError:
                pass
            if buf.strip():
                put(buf.decode("utf-8", errors="replace"))
            put(None)

        threading.Thread(target=reader, name="mcp-stdio-stdin", daemon=True).start()

    def close(self) -> None:
        """停止交回後續的行(閘道端已結束時用;daemon thread 照舊讀到 EOF 或行程結束)。"""
        self._closed = True
        self._queue.put_nowait(None)

    def __aiter__(self) -> _StdinLines:
        return self

    async def __anext__(self) -> str:
        line = None if self._closed else await self._queue.get()
        if line is None or self._closed:
            raise StopAsyncIteration
        return line


class _Stdout:
    """SDK 的 stdout_writer 介面(`await write(str)`、`await flush()`),另提供同步 `write_line`。"""

    def __init__(self, fd: int = 1) -> None:
        self._fd = fd
        self.broken = False

    def write_sync(self, text: str) -> None:
        if self.broken:
            return
        data = text.encode("utf-8")
        try:
            while data:
                n = os.write(self._fd, data)
                data = data[n:]
        except OSError:   # 對方已關閉管線
            self.broken = True

    async def write(self, text: str) -> None:
        self.write_sync(text)

    async def flush(self) -> None:
        return None

    def write_line(self, obj: dict[str, Any]) -> None:
        self.write_sync(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------------------- 轉送

@dataclass
class _State:
    pending: dict[Any, None] = field(default_factory=dict)   # 保留順序的集合
    rejected_status: int | None = None
    eof: bool = False          # stdin 已 EOF
    http_ended: bool = False   # 閘道端的串流自行結束(例如 SDK 吞掉被拒絕的通知 POST 並關閉 read stream)


def _rejection(status: int) -> tuple[str, dict[str, Any]]:
    code = REJECT_CODES[status]
    return code, {"code": JSONRPC_SERVER_ERROR,
                  "message": f"{code}: rejected by gateway (HTTP {status})",
                  "data": {"code": code, "http_status": status}}


def _write_errors(out: _Stdout, state: _State, error: dict[str, Any]) -> None:
    for rid in list(state.pending):
        out.write_line({"jsonrpc": "2.0", "id": rid, "error": error})
    state.pending.clear()


def _status_in(group: BaseExceptionGroup) -> int | None:
    import httpx

    for exc in _leaves(group):
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code
    return None


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _leaves(sub)]
    return [exc]


async def _bridge(url: str, headers: dict[str, str]) -> int:
    # 延遲 import:token/depth 不合法時(結束碼 2)不必載入 httpx、anyio 與 SDK
    import anyio
    import httpx
    from mcp import types
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.stdio import stdio_server

    state = _State()
    out = _Stdout()

    async def on_response(response: httpx.Response) -> None:
        # 只看 POST(GET 通知串流與 DELETE 由 SDK 自行處理);404 也要丟,SDK 對它不丟例外
        if response.request.method == "POST" and response.status_code in REJECT_CODES:
            state.rejected_status = response.status_code
            raise httpx.HTTPStatusError("rejected by gateway", request=response.request, response=response)

    def track(item: Any) -> bool:
        """記錄 request id;解析失敗的項目只在 stderr 記類型,回傳 False(不轉送)。"""
        if isinstance(item, Exception):
            _say(f"stdin 訊息無法解析,已略過({type(item).__name__})")
            return False
        root = item.message.root
        if isinstance(root, types.JSONRPCRequest):
            state.pending[root.id] = None
        return True

    async def stdin_to_http(src: Any, dst: Any, tg: Any) -> None:
        try:
            async for item in src:
                if track(item):
                    await dst.send(item)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):   # SDK 已關閉 write stream
            state.http_ended = True
            tg.cancel_scope.cancel()
            return
        state.eof = True
        with anyio.move_on_after(EOF_DRAIN_TIMEOUT):   # 等進行中的 request 回應
            while state.pending:
                await anyio.sleep(0.02)
        tg.cancel_scope.cancel()

    async def http_to_stdout(src: Any, dst: Any, tg: Any) -> None:
        async for item in src:
            if isinstance(item, Exception):
                _say(f"閘道訊息無法解析,已略過({type(item).__name__})")
                continue
            root = item.message.root
            if isinstance(root, (types.JSONRPCResponse, types.JSONRPCError)):
                state.pending.pop(root.id, None)
            await dst.send(item)
        state.http_ended = True   # 閘道端串流結束(例如通知被拒絕):連 stdin 一起停,見下方
        tg.cancel_scope.cancel()

    async def collect_stdin(src: Any) -> None:
        # 閘道端已結束:收下 stdin 端已讀出、卡在交接的項目(記錄其 request id,稍後回錯誤),讓 SDK 的
        # stdin_reader 結束,stdio context 才能離開
        with anyio.move_on_after(STDIN_DRAIN_TIMEOUT):
            async for item in src:
                track(item)

    client = httpx.AsyncClient(headers=headers, trust_env=False, follow_redirects=False,
                               timeout=httpx.Timeout(30, read=660),
                               event_hooks={"response": [on_response]})
    status: int | None = None
    transport_error: str | None = None
    try:
        async with client:
            stdin = _StdinLines()
            async with stdio_server(stdin=stdin, stdout=out) as (stdio_read, stdio_write):  # type: ignore[arg-type]
                try:
                    async with streamable_http_client(url, http_client=client) as (http_read, http_write, _sid):
                        async with anyio.create_task_group() as tg:
                            tg.start_soon(stdin_to_http, stdio_read, http_write, tg)
                            tg.start_soon(http_to_stdout, http_read, stdio_write, tg)
                finally:
                    stdio_write.close()   # 否則 SDK 的 stdout_writer 一直等,stdio context 不會結束
                    stdin.close()         # 不再交回 stdin 的後續行(閘道端已結束時不等 EOF)
                await collect_stdin(stdio_read)
    except* httpx.HTTPStatusError as eg:
        status = _status_in(eg)
    except* httpx.ConnectError:
        transport_error = "無法連線到閘道(serve 是否已啟動?)"
    except* (httpx.HTTPError, OSError):
        transport_error = "與閘道的連線發生錯誤"
    except* Exception:   # 其他錯誤:固定字串,不帶例外內容(可能含 prompt 或回應)
        transport_error = "轉送時發生內部錯誤"

    if status is None:
        status = state.rejected_status
    if status in REJECT_CODES:
        code, error = _rejection(status)
        _write_errors(out, state, error)
        _say(f"被閘道拒絕:{code}(HTTP {status})")
        return EXIT_REJECTED
    if status is not None or transport_error is not None:
        message = transport_error or f"閘道回應 HTTP {status}"
        _write_errors(out, state, {"code": JSONRPC_SERVER_ERROR, "message": "gateway unavailable"})
        _say(message)
        return EXIT_ERROR
    if state.http_ended:   # 閘道端意外結束(無論是否仍有未回應的 request)
        _write_errors(out, state, {"code": JSONRPC_SERVER_ERROR, "message": "gateway connection closed"})
        _say("閘道連線已結束")
        return EXIT_ERROR
    if state.pending:   # stdin EOF 後 EOF_DRAIN_TIMEOUT 內仍未回應
        _write_errors(out, state, {"code": JSONRPC_SERVER_ERROR,
                                   "message": "stdin closed before the gateway responded"})
        _say(f"stdin 已關閉,{EOF_DRAIN_TIMEOUT:g} 秒內未收到閘道回應的 request 已回錯誤")
        return EXIT_ERROR
    return EXIT_OK


def _quiet_loggers() -> None:
    # ERROR 等級仍會經 logging.lastResort 印到 stderr(SDK 的 logger.exception 帶 traceback 與
    # pydantic input_value,可能含回應文字):不往上傳遞、只掛 NullHandler
    for name in ("mcp", "httpx", "httpcore"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.ERROR)
        lg.propagate = False
        lg.handlers[:] = [logging.NullHandler()]
    # SDK 也會直接寫 root logger(`logging.warning("Failed to validate request: …")`,含 request 片段)
    install_sdk_root_log_filter()


def run(group: str, port: int, environ: Mapping[str, str] | None = None) -> int:
    """`spillway mcp-stdio --group <g> [--port <p>]` 的本體;回傳結束碼。"""
    env = os.environ if environ is None else environ
    try:
        headers = build_headers(env)
    except ConfigError as exc:
        _say(str(exc))
        return EXIT_CONFIG
    _quiet_loggers()
    try:
        return asyncio.run(_bridge(bridge_url(group, port), headers))
    except KeyboardInterrupt:
        return EXIT_SIGINT

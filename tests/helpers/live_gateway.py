"""`live_gateway`:背景 thread 跑真的 `uvicorn.Server`(CHG-20261102-01 T2;AM-5;T3、T4、T8 共用)。

為什麼需要:httpx `ASGITransport` 會把整個回應緩衝完才交回,GET SSE 通知串流永遠拿不到事件;凡需要
GET SSE、`list_changed`、stdio 橋接或停機的測試與 behave 步驟,一律用本 harness(或子行程 serve)。
`ASGITransport` 只用於 HTTP 層的 401/403/404 與 POST 類工具呼叫(見 `tests/integration/mcp/conftest.py`)。

API
---
`start_live_gateway(db_path=None, *, token=True, graceful_timeout=3.0, startup_timeout=10.0,
**create_app_kwargs) -> LiveGateway`
    先以 127.0.0.1:0 綁好 socket 取得隨機埠,以**同一個埠**呼叫 `create_app(port=port, db_path=...,
    **create_app_kwargs)`(Origin 比對與 catalog URL 用它),再在背景 thread 以 `asyncio.run(server.serve(
    sockets=[sock]))` 啟動 uvicorn(`lifespan="on"`、`timeout_graceful_shutdown=graceful_timeout`、
    不裝訊號處理——非主 thread 時 uvicorn 自動略過)。等到 `server.started` 才回傳;啟動失敗 raise。
    - `db_path` 省略時為 `settings.db_path()`(測試的暫存 `SPILLWAY_HOME`)。
    - `token=True`(預設):啟動前先 migration 並以 `settings.rotate_access_token` 產生 token,明文只存在
      `gw.token`(測試行程記憶體);`token=False`:不產生(「尚未產生 token → 401」)。
    - 其餘關鍵字(`clock`、`rng`、`extra_adapter_paths`、`retention_poll_seconds`…)原樣傳給 `create_app`;
      `retention_poll_seconds` 預設 3600(背景清理不干擾測試)。

`live_gateway(...)`:同上參數的 context manager,離開時 `stop()`。

`LiveGateway` 欄位:`app`、`port`、`db_path`、`token`、`server`(`uvicorn.Server`)、`thread`、`loop`
(伺服器的事件迴圈)。
- `base_url` → `"http://127.0.0.1:<port>"`;`url(path)`;`mcp_url(group)`(群組名以 `quote(g, safe="")` 編碼)。
- `auth_headers(token=None)` → `{"Authorization": "Bearer <token>"}`(token 省略時用 `gw.token`;沒有 token 時 `{}`)。
- `http(**kw)` → 同步 `httpx.Client`(base_url、`trust_env=False`;系統代理收不到 Bearer)。
- `async_http(**kw)` → `httpx.AsyncClient`(同上)。
- `db()` → context manager,開一條新連線(已 migration),離開時關閉。
- `rotate_token()` → 直接在資料庫 rotate,更新並回傳 `gw.token`(舊 token 下一次請求即 401)。
- `call_in_loop(fn, *args, timeout=5.0)` → 在伺服器事件迴圈上執行同步函式 `fn(*args)` 並回傳結果
  (讀 SDK 狀態時避免跨 thread 讀到一半)。
- `stream_open(session_id) -> bool` → 在伺服器迴圈上呼叫 `sdk_compat.get_stream_open(app, session_id)`。
- `await wait_stream_open(session_id, timeout=5.0)` → 輪詢到 GET 通知串流在伺服器端就緒(真實時間
  `asyncio.wait_for` 計時;逾時 `TimeoutError`)。
- `mcp_session(group="all", *, token=<gw.token>, headers=None, message_handler=None, read_timeout=60)`
  → async context manager,以官方 SDK(`streamable_http_client` + 自建 `httpx.AsyncClient(trust_env=False)`)
  連 `/mcp/<group>` 並完成 `initialize`,yield `McpClient(session, init, session_id)`:
  `session` 為 `mcp.ClientSession`、`init` 為 `InitializeResult`、`session_id` 為 `Mcp-Session-Id`。
  `token=None` 表示不帶 Authorization;`message_handler` 原樣傳給 `ClientSession`(T3 收通知用)。
  SDK 用戶端在 initialize 後會自己開 GET 通知串流;要確定它已就緒再觸發通知,呼叫
  `wait_stream_open(client.session_id)`。
- `stop(timeout=10.0)` → `server.should_exit = True` 並 join;逾時改設 `force_exit` 再 join。冪等。

注意:測試結束前先離開 `mcp_session`(SDK 用戶端會 DELETE session 並關閉 GET 串流),否則停機要等
`graceful_timeout`。
"""
from __future__ import annotations

import asyncio
import socket
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import uvicorn
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from spillway import settings
from spillway.api.mcp import sdk_compat
from spillway.app import create_app
from spillway.store.db import connect

__all__ = ["LiveGateway", "McpClient", "live_gateway", "start_live_gateway"]

HOST = "127.0.0.1"
_DEFAULT = object()


@dataclass
class McpClient:
    session: ClientSession
    init: types.InitializeResult
    session_id: str | None


@dataclass
class LiveGateway:
    app: Any
    port: int
    db_path: Path
    token: str | None
    server: uvicorn.Server
    thread: threading.Thread | None = None
    loop: asyncio.AbstractEventLoop | None = None
    error: BaseException | None = None
    _stopped: bool = field(default=False, repr=False)

    # ------------------------------------------------------------------ 位址與 HTTP
    @property
    def base_url(self) -> str:
        return f"http://{HOST}:{self.port}"

    def url(self, path: str) -> str:
        return self.base_url + path

    def mcp_url(self, group: str = "all") -> str:
        return self.url(f"/mcp/{quote(group, safe='')}")

    def auth_headers(self, token: str | None = None) -> dict[str, str]:
        tok = token if token is not None else self.token
        return {"Authorization": f"Bearer {tok}"} if tok else {}

    def http(self, **kw: Any) -> httpx.Client:
        kw.setdefault("timeout", 30.0)
        return httpx.Client(base_url=self.base_url, trust_env=False, **kw)

    def async_http(self, **kw: Any) -> httpx.AsyncClient:
        kw.setdefault("timeout", 30.0)
        return httpx.AsyncClient(base_url=self.base_url, trust_env=False, **kw)

    # ------------------------------------------------------------------ 資料庫與 token
    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def rotate_token(self) -> str:
        with self.db() as conn:
            self.token = settings.rotate_access_token(conn)
        return self.token

    # ------------------------------------------------------------------ 伺服器迴圈
    def call_in_loop(self, fn: Callable[..., Any], *args: Any, timeout: float = 5.0) -> Any:
        assert self.loop is not None, "gateway 尚未啟動"

        async def _run() -> Any:
            return fn(*args)

        return asyncio.run_coroutine_threadsafe(_run(), self.loop).result(timeout)

    def stream_open(self, session_id: str) -> bool:
        return bool(self.call_in_loop(sdk_compat.get_stream_open, self.app, session_id))

    async def wait_stream_open(self, session_id: str, timeout: float = 5.0) -> None:
        async def _poll() -> None:
            while not await asyncio.to_thread(self.stream_open, session_id):
                await asyncio.sleep(0.02)

        await asyncio.wait_for(_poll(), timeout)

    # ------------------------------------------------------------------ SDK 用戶端
    @asynccontextmanager
    async def mcp_session(
        self,
        group: str = "all",
        *,
        token: Any = _DEFAULT,
        headers: dict[str, str] | None = None,
        message_handler: Any = None,
        read_timeout: float = 60.0,
    ) -> AsyncIterator[McpClient]:
        hdrs: dict[str, str] = {}
        tok = self.token if token is _DEFAULT else token
        if tok:
            hdrs["Authorization"] = f"Bearer {tok}"
        hdrs.update(headers or {})
        async with httpx.AsyncClient(headers=hdrs, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(30.0, read=read_timeout)) as client:
            async with streamable_http_client(self.mcp_url(group), http_client=client) as (r, w, sid):
                kw: dict[str, Any] = {}
                if message_handler is not None:
                    kw["message_handler"] = message_handler
                async with ClientSession(r, w, **kw) as session:
                    init = await session.initialize()
                    yield McpClient(session=session, init=init, session_id=sid())

    # ------------------------------------------------------------------ 生命週期
    def _serve(self, sock: socket.socket) -> None:
        async def main() -> None:
            self.loop = asyncio.get_running_loop()
            await self.server.serve(sockets=[sock])

        try:
            asyncio.run(main())
        except BaseException as exc:  # noqa: BLE001 — 交給 start()/stop() 回報
            self.error = exc

    def stop(self, timeout: float = 10.0) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout)
            if self.thread.is_alive():
                self.server.force_exit = True
                self.thread.join(timeout)


def start_live_gateway(
    db_path: Path | str | None = None,
    *,
    token: bool = True,
    graceful_timeout: float = 3.0,
    startup_timeout: float = 10.0,
    **create_app_kwargs: Any,
) -> LiveGateway:
    path = Path(db_path) if db_path is not None else settings.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tok: str | None = None
    conn = connect(path)   # migration
    try:
        if token:
            tok = settings.rotate_access_token(conn)
    finally:
        conn.close()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, 0))
    port = sock.getsockname()[1]
    create_app_kwargs.setdefault("retention_poll_seconds", 3600)
    app = create_app(port=port, db_path=path, **create_app_kwargs)
    config = uvicorn.Config(app, host=HOST, port=port, lifespan="on", log_level="warning",
                            access_log=False, timeout_graceful_shutdown=graceful_timeout)
    gw = LiveGateway(app=app, port=port, db_path=path, token=tok, server=uvicorn.Server(config))
    gw.thread = threading.Thread(target=gw._serve, args=(sock,), name=f"live-gateway-{port}", daemon=True)
    gw.thread.start()

    deadline = threading.Event()
    waited = 0.0
    while not gw.server.started:
        if not gw.thread.is_alive():
            sock.close()
            raise RuntimeError(f"live_gateway 啟動失敗:{type(gw.error).__name__ if gw.error else '未知'}") \
                from gw.error
        if waited >= startup_timeout:
            gw.stop()
            raise TimeoutError("live_gateway 啟動逾時")
        deadline.wait(0.02)
        waited += 0.02
    return gw


@contextmanager
def live_gateway(db_path: Path | str | None = None, **kw: Any) -> Iterator[LiveGateway]:
    gw = start_live_gateway(db_path, **kw)
    try:
        yield gw
    finally:
        gw.stop()

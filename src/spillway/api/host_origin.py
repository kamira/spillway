"""全端點 Host/Origin 檢查(CHG-20261102-01 決策 4,決策審議修訂 AM-11;T1 擁有)。

`HostOriginMiddleware(app, port=...)` 是純 ASGI middleware,由 `app.py` 掛在最外層(先於路由、
`/v1`、`/api`、`/mcp`),防 DNS rebinding 與跨站請求:

- 只檢查 `http` 與 `websocket` scope;其他(`lifespan`)原樣通過。
- Host:依 raw `scope["headers"]`,`host` 必須剛好一個,且完整比對
  `^(127\\.0\\.0\\.1|localhost)(:[0-9]{1,5})?$`(不分大小寫);否則 403 `host_not_allowed`
  (含 `[::1]`、`localhost.`、`evil.localhost`、空值、重複、HTTP/1.0 無 Host)。埠號不比對。
- Origin:沒有 `Origin` 一律放行(CLI、SDK 不送);有則必須剛好一個,且完整字串(不分大小寫)
  等於 `http://127.0.0.1:<port>` 或 `http://localhost:<port>`(`<port>` 為 serve 綁定埠);
  結尾斜線、`null`、其他埠一律 403 `origin_not_allowed`。
- 拒絕時直接送出 spec 9.1.2 `error_response(...)` 的 ASGI 回應,不 raise(所以不會變成 500);
  websocket scope 被拒時送 `websocket.close`(code 1008)。

不記錄任何 header 值(可能含 token)。
"""
from __future__ import annotations

import re

from starlette.types import ASGIApp, Receive, Scope, Send

from spillway.api import error_response

__all__ = ["HostOriginMiddleware", "check_host", "check_origin"]

_HOST_RE = re.compile(r"^(127\.0\.0\.1|localhost)(:[0-9]{1,5})?$", re.IGNORECASE)

Headers = list[tuple[bytes, bytes]]


def _values(headers: Headers, name: bytes) -> list[str] | None:
    """raw headers 中名為 `name` 的所有值(ASCII 解碼);任一值不是 ASCII 時回 None(視為不符)。"""
    try:
        return [v.decode("ascii") for k, v in headers if k.lower() == name]
    except UnicodeDecodeError:
        return None


def check_host(headers: Headers) -> bool:
    """raw headers 中 `host` 剛好一個且符合本機規則才回 True。"""
    hosts = _values(headers, b"host")
    if hosts is None or len(hosts) != 1:
        return False
    return _HOST_RE.fullmatch(hosts[0]) is not None


def check_origin(headers: Headers, port: int) -> bool:
    """沒有 `Origin` 放行;有則必須剛好一個且完整(不分大小寫)等於本機 + 綁定埠。"""
    origins = _values(headers, b"origin")
    if origins == []:
        return True
    if origins is None or len(origins) != 1:
        return False
    return origins[0].lower() in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")


class HostOriginMiddleware:
    """純 ASGI;見模組說明。`port` 為 serve 綁定埠(`create_app(port=...)`)。"""

    def __init__(self, app: ASGIApp, *, port: int) -> None:
        self.app = app
        self.port = port

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = list(scope.get("headers") or [])
        if not check_host(headers):
            await self._reject(scope, receive, send, "host_not_allowed",
                               "Host 必須是 127.0.0.1 或 localhost")
            return
        if not check_origin(headers, self.port):
            await self._reject(scope, receive, send, "origin_not_allowed",
                               "Origin 必須是本機的 spillway 位址")
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, code: str, message: str) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": code})
            return
        await error_response(403, code, message)(scope, receive, send)

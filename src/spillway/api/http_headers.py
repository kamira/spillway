"""安全標頭(CHG-20261103-01 T3;決策 7,決策審議修訂 AM-2)。

`SecurityHeadersMiddleware(app)` 是**純 ASGI** middleware(不用 `BaseHTTPMiddleware`,它會干擾 `/mcp`
的 SSE):只處理 `http` scope,在 `http.response.start` 時依 `scope["path"]` 補標頭;同名標頭一律
**覆寫**(先移除既有的同名標頭再加入),不重複附加。

- (a) 路徑為 `/`、`/ui`,或以 `/ui/` 開頭 → `UI_HEADERS`(CSP 含 Trusted Types、nosniff、no-referrer、
  COOP、CORP、`Cache-Control: no-cache`)。
- (b) 以 `/api/` 開頭 → `API_HEADERS`(`Cache-Control: no-store` 與 nosniff;帳本 payload 等回應不留在
  瀏覽器磁碟快取)。
- (c) 其他路徑(`/v1`、`/mcp`)不碰。

`create_app` 在 `add_middleware(HostOriginMiddleware, …)` **之前**加入本 middleware,所以 HostOrigin 在
最外層,本 middleware 在 ExceptionMiddleware 外側:404、405、307、304 都會經過它。HostOrigin 自己回的
403 與 ServerErrorMiddleware 的 500 不帶這些標頭(已知限制)。不讀、不記錄任何請求或回應內容。
"""
from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["API_HEADERS", "CSP", "SecurityHeadersMiddleware", "UI_HEADERS", "headers_for_path"]

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
       "require-trusted-types-for 'script'; trusted-types 'none'")

UI_HEADERS: tuple[tuple[str, str], ...] = (
    ("content-security-policy", CSP),
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
    ("cross-origin-opener-policy", "same-origin"),
    ("cross-origin-resource-policy", "same-origin"),
    ("cache-control", "no-cache"),
)

API_HEADERS: tuple[tuple[str, str], ...] = (
    ("cache-control", "no-store"),
    ("x-content-type-options", "nosniff"),
)


def headers_for_path(path: str) -> tuple[tuple[str, str], ...]:
    """依路徑決定要補的標頭;不在 (a)、(b) 範圍時回空 tuple。"""
    if path in ("/", "/ui") or path.startswith("/ui/"):
        return UI_HEADERS
    if path.startswith("/api/"):
        return API_HEADERS
    return ()


class SecurityHeadersMiddleware:
    """純 ASGI;見模組說明。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        extra = headers_for_path(scope.get("path") or "")
        if not extra:
            await self.app(scope, receive, send)
            return
        names = {name.encode("latin-1") for name, _ in extra}
        encoded = [(name.encode("latin-1"), value.encode("latin-1")) for name, value in extra]

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                kept = [(k, v) for k, v in message.get("headers") or [] if k.lower() not in names]
                message = {**message, "headers": kept + encoded}
            await send(message)

        await self.app(scope, receive, send_with_headers)

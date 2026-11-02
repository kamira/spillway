"""`/mcp/<group>` 的 ASGI wrapper:驗證順序與 session 綁群組(CHG-20261102-01 T2;決策 9、10;AM-4、AM-10)。

`McpEndpoint(app)` 由 `app.py` 以 `app.mount("/mcp", ...)` 掛載;外層的 `HostOriginMiddleware`(決策 4,
T1)已先檢查 Host/Origin(順序 ①)。這裡對 `/mcp/*` 的**每一個方法**(POST、GET、DELETE)依序:

② depth:所有 `x-spillway-depth` 值中只要有任一個不符 `^[0-9]+$`(含空字串、`-1`、`+1`),或數值 ≥ 1
   → 403 `recursion_blocked`;只有全部為 `0`(或未帶)才放行。在驗證 token 之前判斷。
③ Bearer:`authorization` 必須剛好一個,scheme 比對不分大小寫;token 雜湊**每次請求**從資料庫讀
   (`settings.get_access_token_hash`,rotate 立即生效);尚未產生 token(None)時先回 401 再做比對;
   `hmac.compare_digest(sha256(t), 雜湊)`。缺 header、格式錯、雜湊不符或尚未產生 token → 401
   `unauthorized`,帶 `WWW-Authenticate: Bearer`(不帶 `resource_metadata`)。
④ 停機中(`app.state.shutting_down`)或 lifespan 外(`app.state.mcp_manager` 為 None)→ 503 `shutting_down`。
⑤ 群組:以 `starlette.routing.get_route_path(scope)` 取 mount 之後的路徑,必須剛好是 `/<group>`
   (否則 404 `not_found`);不帶 `Mcp-Session-Id`(開新 session)時群組必須存在或為 `all`,否則 404
   `not_found`。帶 session id 的請求交給 SDK 判斷(群組在 session 期間被刪時既有 session 仍可用,
   `tools/list` 回空、`ask` 回 `route_not_found`,決策 9)。

以上皆為 spec 9.1.2 JSON。通過後設 `scope["user"] = AuthenticatedUser(AccessToken(token="",
client_id=f"spillway-group:{group}", scopes=[]))` 與 `scope["spillway.group"] = group`,交給 SDK 的
`StreamableHTTPSessionManager.handle_request`(在處理請求時才讀 `app.state.mcp_manager`)。SDK 以
`scope["user"]` 記錄 session 擁有者:同一個 session id 改打別的群組 → SDK 回 404(body 為 SDK 的
JSON-RPC 格式,例外於 9.1.2)。工具 handler 由 `request_context.request.scope["spillway.group"]` 取得群組。

秘密:不記錄任何 header 值;token 明文只在本次比對時存在於記憶體。
"""
from __future__ import annotations

import hmac
import re
from typing import Any

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from starlette.routing import get_route_path
from starlette.types import Receive, Scope, Send

from spillway import settings
from spillway.api import error_response, redact_echo

from .tools import group_exists

__all__ = ["McpEndpoint", "GROUP_SCOPE_KEY", "CLIENT_ID_PREFIX", "check_depth", "check_bearer"]

GROUP_SCOPE_KEY = "spillway.group"
CLIENT_ID_PREFIX = "spillway-group:"

_DEPTH_RE = re.compile(r"[0-9]+")
_PATH_RE = re.compile(r"/([^/]+)")

Headers = list[tuple[bytes, bytes]]


def _values(headers: Headers, name: bytes) -> list[str] | None:
    """raw headers 中名為 `name` 的所有值;任一值不是 ASCII 時回 None(視為不符)。"""
    try:
        return [v.decode("ascii") for k, v in headers if k.lower() == name]
    except UnicodeDecodeError:
        return None


def check_depth(headers: Headers) -> bool:
    """② 遞迴防護:未帶或全部為 0 才回 True。"""
    values = _values(headers, b"x-spillway-depth")
    if values is None:
        return False
    return all(_DEPTH_RE.fullmatch(v) is not None and int(v) == 0 for v in values)


def check_bearer(headers: Headers, stored_hash: str | None) -> bool:
    """③ Bearer:尚未產生 token、header 缺漏或重複、scheme 不是 bearer、雜湊不符 → False。"""
    if stored_hash is None:   # 未產生 token:先拒絕,不做比對(None 會讓 compare_digest TypeError)
        return False
    values = _values(headers, b"authorization")
    if values is None or len(values) != 1:
        return False
    scheme, _, token = values[0].strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return False
    return hmac.compare_digest(settings.hash_access_token(token), stored_hash)


class McpEndpoint:
    """純 ASGI;見模組說明。`app` 為 spillway 的 FastAPI app(讀 `app.state`)。"""

    def __init__(self, app: Any) -> None:
        self.spillway_app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":   # websocket 不支援;lifespan 不會進到 mount
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008, "reason": "not_found"})
            return
        state = self.spillway_app.state
        headers = list(scope.get("headers") or [])

        if not check_depth(headers):
            await error_response(403, "recursion_blocked",
                                 "偵測到遞迴呼叫(X-Spillway-Depth 必須為 0 或不帶)")(scope, receive, send)
            return

        conn = state.conn_factory()
        try:
            stored = settings.get_access_token_hash(conn)
        finally:
            conn.close()
        if not check_bearer(headers, stored):
            await error_response(401, "unauthorized", "需要有效的 Authorization: Bearer <token>",
                                 headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
            return

        manager = getattr(state, "mcp_manager", None)
        if state.shutting_down or manager is None:
            await error_response(503, "shutting_down", "閘道停機中,請稍後重試")(scope, receive, send)
            return

        m = _PATH_RE.fullmatch(get_route_path(scope))
        group = m.group(1) if m else None
        # 群組存在性只擋「開新 session」的請求:session 期間群組被刪時,既有 session 照常轉給 SDK
        # (`tools/list` 回空、`ask` 回 route_not_found;決策 9)。帶 session id 的請求若 id 不存在,或
        # 屬於別的群組(擁有者不符),由 SDK 回 404。
        if group is not None and not _values(headers, b"mcp-session-id"):
            conn = state.conn_factory()
            try:
                if not group_exists(conn, group):
                    group = None
            finally:
                conn.close()
        if group is None:
            await error_response(404, "not_found",
                                 f"找不到 MCP 群組端點:{redact_echo(get_route_path(scope))}"
                                 )(scope, receive, send)
            return

        scope = dict(scope)
        scope["user"] = AuthenticatedUser(
            AccessToken(token="", client_id=f"{CLIENT_ID_PREFIX}{group}", scopes=[]))
        scope[GROUP_SCOPE_KEY] = group
        await manager.handle_request(scope, receive, send)

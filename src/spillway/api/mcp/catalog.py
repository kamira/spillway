"""UI-8 的資料來源 `mcp_catalog(app, group="all")`(CHG-20261102-01 T2;決策 7、8;AM-3、AM-13)。

純資料、無副作用(只讀資料庫);本筆不加 HTTP 端點(M4 決定掛在哪)。內容只限 MCP 介面。

回傳 `{group, connection, tools, notifications, errors}`:
- `connection`:`transport`(`streamable-http`)、`url`(本群組端點)、`groups`(`all` 在前、其餘依 name;
  每項 `{name, url}`)、`auth`(`Bearer`、header、環境變數 `SPILLWAY_TOKEN`)、`stdio`(`spillway mcp-stdio`
  的用法)。URL 主機固定 `127.0.0.1`、埠為 `app.state.port`;群組名以 `quote(g, safe="")` 編碼。
- `tools`:與 `/mcp/<group>` 的 `tools/list` 同一份定義(`tools.tool_definitions`),以
  `Tool.model_dump(mode="json", by_alias=True, exclude_none=True)` 輸出;群組不存在時為空清單。
- `notifications`:`notifications/tools/list_changed`。
- `errors`:三類(決策 8):`http`(spec 9.1.2 JSON)、`tool`(`CallToolResult(isError)` 的
  `structuredContent.error.code`)、`sdk_transport`(SDK 產生,JSON-RPC 或純文字,不是 9.1.2)。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from spillway.store import repo

from .tools import ALL_GROUP, tool_definitions

__all__ = ["mcp_catalog", "HTTP_ERRORS", "TOOL_ERRORS", "SDK_TRANSPORT_ERRORS"]

HTTP_ERRORS: tuple[dict[str, Any], ...] = (
    {"code": "host_not_allowed", "status": 403, "format": "spec-9.1.2",
     "description": "Host 不是 127.0.0.1 或 localhost(防 DNS rebinding)"},
    {"code": "origin_not_allowed", "status": 403, "format": "spec-9.1.2",
     "description": "帶了 Origin 且不是本機的 spillway 位址(跨站請求)"},
    {"code": "recursion_blocked", "status": 403, "format": "spec-9.1.2",
     "description": "X-Spillway-Depth 不是 0(遞迴防護,在驗證 token 之前判斷)"},
    {"code": "unauthorized", "status": 401, "format": "spec-9.1.2",
     "description": "缺少或錯誤的 Authorization: Bearer token,或尚未產生 token;帶 WWW-Authenticate: Bearer"},
    {"code": "shutting_down", "status": 503, "format": "spec-9.1.2",
     "description": "閘道停機中或尚未啟動完成"},
    {"code": "not_found", "status": 404, "format": "spec-9.1.2",
     "description": "群組端點不存在(群組名不是既有群組也不是 all)"},
)

TOOL_ERRORS: tuple[dict[str, Any], ...] = (
    {"code": "route_not_found", "description": "route 不存在,或 session 綁定的群組已被刪除"},
    {"code": "no_candidate", "description": "沒有可用的候選成員;structuredContent.attempts 列出每次嘗試"},
    {"code": "shutting_down", "description": "停機期滿,進行中的 ask 已取消"},
    {"code": "validation_error",
     "description": "參數不合法:route 不是本群組可見的 route、prompt 為空、effort 不在五級內或多餘參數"},
)

SDK_TRANSPORT_ERRORS: tuple[dict[str, Any], ...] = (
    {"status": 400, "message": "Invalid Content-Type / Missing session ID", "format": "json-rpc",
     "description": "請求格式不符 Streamable HTTP(SDK 產生,不是 spec 9.1.2)"},
    {"status": 404, "message": "Session not found", "format": "json-rpc",
     "description": "session 不存在、已結束,或以同一 session id 改打別的群組"},
    {"status": 406, "message": "Not Acceptable", "format": "json-rpc",
     "description": "Accept 未同時接受 application/json 與 text/event-stream"},
    {"status": 413, "message": "Request body too large", "format": "text",
     "description": "請求本體超過 SDK 上限"},
    {"status": 503, "message": "Too many open sessions", "format": "json-rpc",
     "description": "同時開啟的 session 已達上限(100)"},
)

_NOTIFICATIONS: tuple[dict[str, Any], ...] = (
    {"method": "notifications/tools/list_changed",
     "description": "route 或群組變更後送出;用戶端應重新呼叫 tools/list",
     "delivery": "只送達已開啟 GET 通知串流、且列過或呼叫過工具的 session"},
)


def _url(port: int, group: str) -> str:
    return f"http://127.0.0.1:{port}/mcp/{quote(group, safe='')}"


def mcp_catalog(app: Any, group: str = ALL_GROUP) -> dict[str, Any]:
    port = int(app.state.port)
    conn = app.state.conn_factory()
    try:
        names = [g.name for g in repo.list_groups(conn)]
        tools = [t.model_dump(mode="json", by_alias=True, exclude_none=True)
                 for t in tool_definitions(conn, group)]
    finally:
        conn.close()
    return {
        "group": group,
        "connection": {
            "transport": "streamable-http",
            "url": _url(port, group),
            "groups": [{"name": n, "url": _url(port, n)} for n in [ALL_GROUP, *sorted(names)]],
            "auth": {"type": "Bearer", "header": "Authorization", "value": "Bearer ${SPILLWAY_TOKEN}",
                     "env": "SPILLWAY_TOKEN"},
            "stdio": {"command": "spillway", "args": ["mcp-stdio", "--group", group, "--port", str(port)],
                      "env": ["SPILLWAY_TOKEN"],
                      "description": "給不支援 HTTP 的工具:stdio 與本群組端點之間的轉送"},
        },
        "tools": tools,
        "notifications": [dict(n) for n in _NOTIFICATIONS],
        "errors": {
            "http": [dict(e) for e in HTTP_ERRORS],
            "tool": [dict(e) for e in TOOL_ERRORS],
            "sdk_transport": [dict(e) for e in SDK_TRANSPORT_ERRORS],
        },
    }

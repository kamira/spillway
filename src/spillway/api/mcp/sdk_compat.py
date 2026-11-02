"""SDK 私有屬性的集中存取點(CHG-20261102-01 決策 14;AM-5)。

本專案對 `mcp` SDK 私有屬性的存取**只可**寫在這個模組;每個函式在
`tests/integration/mcp/test_sdk_compat.py` 有一個絆線測試,SDK 升版改動內部結構時會失敗。

- `get_stream_open(app, session_id) -> bool`(T2):該 session 的 GET 通知串流是否已在伺服器端就緒——
  讀 `app.state.mcp_manager._server_instances[session_id]._request_streams` 是否含
  `mcp.server.streamable_http.GET_STREAM_KEY`。manager 不存在(lifespan 外)、session 不存在時回 False。
  測試與 behave 步驟用它輪詢「開啟通知串流」(等待以真實時間 `asyncio.wait_for` 計時)。
  只讀、不改變任何狀態;在伺服器的事件迴圈之外呼叫也只是讀 dict(`live_gateway` 另提供在迴圈內呼叫的方式)。
- `iter_transports(app) -> list[StreamableHTTPServerTransport]`(T3):目前 session manager 的全部
  transport(讀 `app.state.mcp_manager._server_instances.values()` 的快照);manager 不存在時為空清單。
  停機掛鉤以它對每個 transport 呼叫公開的 `close_standalone_sse_stream()`(決策 14 ③)。
"""
from __future__ import annotations

from typing import Any

from mcp.server.streamable_http import GET_STREAM_KEY

__all__ = ["get_stream_open", "iter_transports"]


def iter_transports(app: Any) -> list[Any]:
    manager = getattr(app.state, "mcp_manager", None)
    if manager is None:
        return []
    return list(manager._server_instances.values())   # SDK 私有(mcp 1.30)


def get_stream_open(app: Any, session_id: str) -> bool:
    manager = getattr(app.state, "mcp_manager", None)
    if manager is None:
        return False
    transport = manager._server_instances.get(session_id)   # SDK 私有(mcp 1.30)
    if transport is None:
        return False
    return GET_STREAM_KEY in transport._request_streams     # SDK 私有(mcp 1.30)

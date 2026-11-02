"""`notifications/tools/list_changed`(CHG-20261102-01 T3;決策 13;AM-9)。

`McpEvents`:`app.state.mcp_events` 的實作(取代 T2 的 `NoopMcpEvents`)。
- `register(session)`:`SpillwayServer` 在 `tools/list` 與 `tools/call` handler 內以公開的
  `server.request_context.session` 登記;以 `weakref.WeakSet` 保存,session 結束後自動消失。
  沒列過工具也沒呼叫過工具的 session 沒有工具清單要更新,不登記。
- `changed()`:**同步**函式,立即返回。對每個登記的 session 以背景 task 執行
  `send_tool_list_changed()`(外包 `anyio.move_on_after(NOTIFY_TIMEOUT)`,保留 task 參照直到結束);
  吞掉 `ClosedResourceError`/`BrokenResourceError`(session 正在關閉),其他例外只記類型——不拖住、
  也不讓呼叫端(管理 API)失敗。不在事件迴圈內呼叫時什麼都不做。
- 對**所有**登記的 session 送通知(不區分群組;用戶端重取 `tools/list` 時才依群組重算)。
  沒有開啟 GET SSE 通知串流的 session 收不到推送(SDK 行為:沒有 related request 的訊息只走 GET 串流),
  用戶端下次 `tools/list` 仍得到新清單。

呼叫端:`api.admin` 的 route 與群組 CRUD、`PUT/DELETE /api/routes/{name}/order`,一律在交易 commit
之後才呼叫。秘密:不記錄任何內容,例外只記類型。
"""
from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Any

import anyio

__all__ = ["McpEvents", "NOTIFY_TIMEOUT"]

_log = logging.getLogger(__name__)

NOTIFY_TIMEOUT = 2.0   # 單一 session 送出通知的上限(秒)


class McpEvents:
    """見模組說明。"""

    def __init__(self) -> None:
        self._sessions: weakref.WeakSet[Any] = weakref.WeakSet()
        self._tasks: set[asyncio.Task[None]] = set()

    def register(self, session: Any) -> None:
        if session is not None:
            self._sessions.add(session)

    @property
    def sessions(self) -> list[Any]:
        """目前登記(且尚未被回收)的 session。"""
        return list(self._sessions)

    @property
    def pending(self) -> set[asyncio.Task[None]]:
        """尚未結束的通知 task(測試用)。"""
        return {t for t in self._tasks if not t.done()}

    def changed(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:   # 不在事件迴圈內(沒有可送通知的連線)
            return
        for session in list(self._sessions):
            task = loop.create_task(_notify(session), name="spillway-mcp-list-changed")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)


async def _notify(session: Any) -> None:
    try:
        with anyio.move_on_after(NOTIFY_TIMEOUT):
            await session.send_tool_list_changed()
    except (anyio.ClosedResourceError, anyio.BrokenResourceError):
        pass   # session 正在關閉或已關閉
    except Exception as exc:  # noqa: BLE001 — 通知失敗不影響管理 API
        _log.warning("tools/list_changed 通知失敗:%s", type(exc).__name__)

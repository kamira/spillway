"""MCP server 本體與 session manager 的生命週期(CHG-20261102-01 T2;決策 9、13;AM-2、AM-4)。

`SpillwayServer(app)`:官方 SDK 低階 `mcp.server.lowlevel.Server` 的子類。`create_app` 建立一個實例,存為
`app.state.mcp_server`(不持有逐 session 狀態;工具定義每次依請求群組產生)。
- `tools/list`:`tools.tool_definitions(conn, 本請求群組)`。
- `tools/call`:以 `call_tool(validate_input=False)` 註冊(決策 11),交 `tools.call_tool`。
- 本請求群組:`current_group()` = `request_context.request.scope["spillway.group"]`(由 `auth.McpEndpoint`
  設定)。
- `create_initialization_options()` 預設 `NotificationOptions(tools_changed=True)`,initialize 宣告
  `tools.listChanged=true`(決策 13、AM-2)。`notifications/tools/list_changed` 的實際送出由 T3 的
  `api/mcp/events.py` 取代 `app.state.mcp_events` 後提供;T2 先放 `NoopMcpEvents`。

`McpRuntime`:session manager 的生命週期(AM-4)。lifespan 每次進入時 `await McpRuntime.start(app)`:
新建 `StreamableHTTPSessionManager(app=server, json_response=False, stateless=False,
security_settings=None, max_sessions=100)`(SDK 的 DNS rebinding 檢查關閉,由 T1 的 Host/Origin
middleware 負責;`run()` 每個實例只能呼叫一次,所以每次 lifespan 都新建),以 `asyncio.create_task`
啟動**專用持有 task**(task 內 `async with mgr.run(): await stop_event.wait()`),就緒後才存到
`app.state.mcp_manager`。lifespan 關閉時 `await runtime.stop()`:先把 `app.state.mcp_manager` 設回 None
(之後的 `/mcp` 請求 → 503 `shutting_down`),再 set 停止事件並 await 持有 task(`mgr.run()` 結束時取消
剩餘的 session 與工具 task)。因為 anyio 的 cancel scope 只在持有 task 內進出,lifespan 的 enter 與 exit
可以在不同 task(behave `Gateway` 以兩次 `asyncio.Runner.run` 進出 lifespan)。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from . import tools as mcp_tools
from .auth import GROUP_SCOPE_KEY

__all__ = ["SpillwayServer", "McpRuntime", "NoopMcpEvents", "SERVER_NAME", "MAX_SESSIONS",
           "STOP_TIMEOUT"]

_log = logging.getLogger(__name__)

SERVER_NAME = "spillway"
MAX_SESSIONS = 100
STOP_TIMEOUT = 10.0   # 停止持有 task 的等待上限(秒);逾時就取消它


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("spillway")
    except Exception:  # noqa: BLE001 — 未安裝(PYTHONPATH=src)時
        return "0"


class NoopMcpEvents:
    """`app.state.mcp_events` 的預設值(決策 13):`changed()` 什麼都不做;由 T3 的 `api/mcp/events.py` 取代。"""

    def changed(self) -> None:
        return None


class SpillwayServer(Server):
    """見模組說明。`app` 為 spillway 的 FastAPI app。"""

    def __init__(self, app: Any) -> None:
        super().__init__(SERVER_NAME, version=_version(),
                         instructions="spillway 閘道:以 ask 讓本群組的 route 處理 prompt;"
                                      "list_routes 與 quota_status 查看 route 順序與成員額度。")
        self.spillway_app = app

        @self.list_tools()
        async def _list_tools() -> list[types.Tool]:
            conn = app.state.conn_factory()
            try:
                return mcp_tools.tool_definitions(conn, self.current_group())
            finally:
                conn.close()

        @self.call_tool(validate_input=False)
        async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            return await mcp_tools.call_tool(app, self.current_group(), name, arguments)

    def current_group(self) -> str:
        """本請求的群組(`auth.McpEndpoint` 設在 scope)。不在 HTTP 請求內呼叫時丟 LookupError。"""
        request = self.request_context.request
        if request is None or GROUP_SCOPE_KEY not in request.scope:
            raise LookupError("沒有 MCP 請求的群組")
        return request.scope[GROUP_SCOPE_KEY]

    def create_initialization_options(
        self,
        notification_options: NotificationOptions | None = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
    ) -> InitializationOptions:
        return super().create_initialization_options(
            notification_options or NotificationOptions(tools_changed=True),
            experimental_capabilities,
        )


class McpRuntime:
    """一次 lifespan 的 session manager 與其持有 task(見模組說明)。"""

    def __init__(self, app: Any, manager: StreamableHTTPSessionManager) -> None:
        self.app = app
        self.manager = manager
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    @classmethod
    async def start(cls, app: Any) -> McpRuntime:
        manager = StreamableHTTPSessionManager(
            app=app.state.mcp_server, json_response=False, stateless=False,
            security_settings=None, max_sessions=MAX_SESSIONS)
        rt = cls(app, manager)
        rt.task = asyncio.create_task(rt._hold(), name="spillway-mcp-manager")
        ready = asyncio.ensure_future(rt._ready.wait())
        try:
            await asyncio.wait({rt.task, ready}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            ready.cancel()
        if not rt._ready.is_set():   # 持有 task 在就緒前結束:把它的例外往上丟
            await rt.task
            raise RuntimeError("MCP session manager 未能啟動")
        app.state.mcp_manager = manager
        return rt

    async def _hold(self) -> None:
        async with self.manager.run():
            self._ready.set()
            await self._stop.wait()

    async def stop(self) -> None:
        """lifespan 關閉第 1 步:之後的 `/mcp` 請求回 503;停止持有 task(取消剩餘 session 與工具 task)。"""
        if self.app.state.mcp_manager is self.manager:
            self.app.state.mcp_manager = None
        self._stop.set()
        task = self.task
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), STOP_TIMEOUT)
        except asyncio.TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        except Exception as exc:  # noqa: BLE001 — 關閉時不讓 SDK 的例外中斷 lifespan 其餘步驟
            _log.error("MCP session manager 結束時發生例外:%s", type(exc).__name__)

"""MCP server(CHG-20261102-01;spec 8.1;design D6、D9)。

- `server.py`:`SpillwayServer`(SDK 低階 Server 子類)、`McpRuntime`(每次 lifespan 新建 session manager
  與專用持有 task)、`NoopMcpEvents`(`app.state.mcp_events` 的預設值;T3 取代)。
- `auth.py`:掛在 `/mcp` 的 ASGI wrapper `McpEndpoint`(depth → Bearer → 停機 → 群組;session 綁群組)。
- `tools.py`:工具 `ask`、`list_routes`、`quota_status` 的定義與實作。
- `catalog.py`:UI-8 的資料來源 `mcp_catalog(app, group="all")`。
- `sdk_compat.py`:SDK 私有屬性的唯一存取點(`get_stream_open`)。

分層(Global Constraints):本套件只經 `Router.route`、`api.drain.run_until_drain`、`spillway.routing` 的
公開純函式、`spillway.quota.member_quota_rows`、`store.repo` 公開函式與 `app.state` 取得資料;不直接
呼叫 adapter、不引用 `api.admin` 內部;SQL 只在 `store`。
"""
from __future__ import annotations

from .auth import McpEndpoint
from .catalog import mcp_catalog
from .server import McpRuntime, NoopMcpEvents, SpillwayServer

__all__ = ["mcp_catalog", "McpEndpoint", "McpRuntime", "NoopMcpEvents", "SpillwayServer"]

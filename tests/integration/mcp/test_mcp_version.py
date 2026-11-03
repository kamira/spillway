"""MCP `serverInfo.version` 與 `GET /api/settings` 的 `version` 同源(CHG-20261103-01 T1;決策 21 ③)。"""
from __future__ import annotations

import pytest

from spillway.settings import package_version

pytestmark = pytest.mark.anyio


async def test_server_info_version_equals_settings_version(live_gateway):
    async with live_gateway.async_http() as http:
        version = (await http.get("/api/settings")).json()["version"]
    async with live_gateway.mcp_session("all") as c:
        assert c.init.serverInfo.version == version == package_version()

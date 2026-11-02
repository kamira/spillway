"""`live_gateway` harness 本身(CHG-20261102-01 T2 L2;AM-5)。

- 能啟停;`create_app(port)` 與實際監聽埠相同。
- 經 Host/Origin middleware 收到**未緩衝**的 SSE:SDK 用戶端 initialize 後開的 GET 通知串流,在伺服器端
  以 `sdk_compat.get_stream_open` 輪詢得到「已就緒」(以 ASGITransport 時這條串流永遠不會出現)。
- 同一個 app 先後兩次 harness 不互相影響(各自新埠)。
"""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.anyio


async def test_start_stop_and_port(live_gateway):
    gw = live_gateway
    assert gw.app.state.port == gw.port
    with gw.http() as c:
        r = c.get("/api/settings/context")
        assert r.status_code == 200, r.text
        r = c.get("/api/settings/context", headers={"Origin": f"http://127.0.0.1:{gw.port}"})
        assert r.status_code == 200
        r = c.get("/api/settings/context", headers={"Origin": "http://127.0.0.1:1"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "origin_not_allowed"
    assert gw.app.state.mcp_manager is not None
    gw.stop()
    assert not gw.thread.is_alive()
    assert gw.app.state.mcp_manager is None   # lifespan 已結束
    gw.stop()   # 冪等


async def test_get_sse_stream_is_not_buffered(live_gateway):
    gw = live_gateway
    async with gw.mcp_session("all") as client:
        assert client.session_id
        await gw.wait_stream_open(client.session_id, timeout=5.0)
        assert gw.stream_open(client.session_id) is True
        assert gw.stream_open("no-such-session") is False
    # 離開後 SDK 用戶端 DELETE session:伺服器端不再有這條串流
    await asyncio.wait_for(_until_closed(gw, client.session_id), 5.0)


async def _until_closed(gw, sid: str) -> None:
    while await asyncio.to_thread(gw.stream_open, sid):
        await asyncio.sleep(0.02)


async def test_restart_on_same_database(start_gateway):
    a = start_gateway()
    a.stop()
    b = start_gateway()   # 同一資料庫再啟動一次:新 app、新埠、token 重新產生
    assert b.app is not a.app and b.token != a.token
    with b.http() as http:
        assert http.get("/api/groups").status_code == 200
    async with b.mcp_session("all") as client:
        assert client.init.serverInfo.name == "spillway"

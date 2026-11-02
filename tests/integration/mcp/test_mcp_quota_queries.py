"""`quota_status` 與 `/api/dashboard` 的查詢數相同(CHG-20261102-01 T2 L1;決策 12;AM-12)。

兩邊都經 `spillway.quota.member_quota_rows`;以 `sqlite3.Connection.set_trace_callback` 計數。`all` 群組
不另查群組與成員,所以兩邊完全相同;具名群組另以 `repo.get_group` 與 `repo.list_deployments` 取選擇器
與成員 tags(與 `GET /api/groups/{name}` 的預覽同一來源;不在 NFR-2 的範圍)。
`quota_status` 直接呼叫 `api.mcp.tools.call_tool`(行程內,與 SDK 的 `tools/call` 同一個進入點)。
"""
from __future__ import annotations

import pytest

from spillway.api.mcp import tools
from spillway.store import repo
from tests.helpers import api_member

pytestmark = pytest.mark.anyio


def _traced(app, sink: list[str]):
    original = app.state.conn_factory

    def factory():
        conn = original()
        conn.set_trace_callback(sink.append)
        return conn

    return original, factory


async def _count_dashboard(h) -> int:
    sink: list[str] = []
    original, factory = _traced(h.app, sink)
    h.app.state.conn_factory = factory
    try:
        r = await h.client.get("/api/dashboard")
        assert r.status_code == 200
    finally:
        h.app.state.conn_factory = original
    return len(sink)


async def _count_quota(h, group: str) -> tuple[int, dict]:
    sink: list[str] = []
    original, factory = _traced(h.app, sink)
    h.app.state.conn_factory = factory
    try:
        r = await tools.call_tool(h.app, group, "quota_status", {})
    finally:
        h.app.state.conn_factory = original
    assert r.isError is False
    return len(sink), r.structuredContent


async def test_query_counts_equal(asgi_app, db):
    for i in range(5):
        repo.save_deployment(db, api_member(f"m{i}", "http://127.0.0.1:9", tags=("a",)))
    repo.save_group(db, repo.Group("coding", {"all": ["a"]}))
    db.commit()
    dash = await _count_dashboard(asgi_app)
    q_all, body = await _count_quota(asgi_app, "all")
    assert q_all == dash
    assert [m["id"] for m in body["members"]] == [f"m{i}" for i in range(5)]

    for i in range(5, 15):
        repo.save_deployment(db, api_member(f"m{i}", "http://127.0.0.1:9", tags=("a",)))
    db.commit()
    assert await _count_dashboard(asgi_app) == dash          # 與成員數無關
    assert (await _count_quota(asgi_app, "all"))[0] == dash
    _, body2 = await _count_quota(asgi_app, "coding")
    assert len(body2["members"]) == 15

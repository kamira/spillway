"""`GET /api/mcp/catalog`(CHG-20261103-01 T2;決策 10 ⑤;AM-13)與 T2 新端點的 L2。

L1:回應 JSON 等於 `mcp_catalog(app, group)`(預設 `all`);不存在的群組 → 404 `not_found` 且不呼叫
`mcp_catalog`;回應不含 `/api/`、`/v1`、token 與雜湊。
L2:以 `start_live_gateway` 的真實 uvicorn 確認 T2 新端點的 Host/Origin 檢查照常(錯誤 Origin → 403),
GET 端點不改變資料庫狀態(全表快照)。
"""
from __future__ import annotations

import json

import pytest

from spillway import settings
from spillway.api.admin import mcp_config
from spillway.api.mcp.catalog import mcp_catalog
from spillway.store import repo

from tests.helpers.live_gateway import live_gateway
from tests.helpers.ui import db_snapshot

from .helpers import api_member, assert_error

pytestmark = pytest.mark.anyio

CATALOG = "/api/mcp/catalog"


async def _seed(client) -> None:
    assert (await client.post("/api/deployments", json=api_member("a", tags=["coding"]))).status_code == 201
    assert (await client.post("/api/groups", json={"name": "coding",
                                                   "selector": {"any": ["coding"]}})).status_code == 201
    assert (await client.post("/api/routes", json={"name": "review",
                                                   "target": {"group": "coding"}})).status_code == 201
    assert (await client.post("/api/routes", json={"name": "other",
                                                   "target": {"tags": {"all": ["x"]}}})).status_code == 201


async def test_catalog_equals_mcp_catalog(make_app, make_client):
    app = make_app()
    async with make_client(app) as c:
        await _seed(c)
        r = await c.get(CATALOG)
        assert r.status_code == 200, r.text
        assert r.json() == json.loads(json.dumps(mcp_catalog(app, "all")))
        assert r.json()["group"] == "all"
        r = await c.get(CATALOG, params={"group": "coding"})
        assert r.status_code == 200, r.text
        assert r.json() == json.loads(json.dumps(mcp_catalog(app, "coding")))
        assert r.json()["group"] == "coding"
        assert (await c.get(CATALOG, params={"group": "all"})).json() == \
            json.loads(json.dumps(mcp_catalog(app, "all")))


async def test_catalog_unknown_group_404_without_calling_catalog(client, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(mcp_config, "mcp_catalog", lambda app, group: calls.append(group) or {})
    assert_error(await client.get(CATALOG, params={"group": "ghost"}), 404, "not_found")
    assert calls == []
    assert (await client.get(CATALOG)).status_code == 200
    assert calls == ["all"]


async def test_catalog_has_no_api_paths_or_secrets(client, db):
    await _seed(client)
    token = settings.rotate_access_token(db)
    digest = repo.get_setting(db, settings.ACCESS_TOKEN_KEY)
    for params in ({}, {"group": "coding"}):
        text = (await client.get(CATALOG, params=params)).text
        assert "/api/" not in text and "/v1" not in text
        assert token not in text and digest not in text


async def test_catalog_does_not_write(client, db):
    await _seed(client)
    before = list(db.iterdump())
    assert (await client.get(CATALOG)).status_code == 200
    assert (await client.get(CATALOG, params={"group": "coding"})).status_code == 200
    assert list(db.iterdump()) == before


# ---------------------------------------------------------------- L2:真實 uvicorn


def test_new_endpoints_host_origin_and_read_only(tmp_path, fake_api):
    with live_gateway(tmp_path / "live.db") as gw:
        with gw.http() as http:
            assert http.post("/api/deployments", json=api_member(
                "a", tags=["coding"], fake_api=fake_api)).status_code == 201
            assert http.post("/api/groups", json={"name": "coding",
                                                  "selector": {"any": ["coding"]}}).status_code == 201
            assert http.post("/api/routes", json={"name": "rt",
                                                  "target": {"group": "coding"}}).status_code == 201
            requests = [("GET", "/api/routes/rt/effective-order", None),
                        ("GET", CATALOG, None),
                        ("GET", CATALOG + "?group=coding", None),
                        ("POST", "/api/groups/preview", {"selector": {"any": ["coding"]}})]
            before = db_snapshot(gw)
            same = {"Origin": gw.base_url}
            for method, path, body in requests:
                r = http.request(method, path, json=body, headers={"Origin": "http://evil.example"})
                assert r.status_code == 403, (path, r.status_code, r.text)
                assert set(r.json()) == {"error"}
                r = http.request(method, path, json=body, headers={"Origin": "http://127.0.0.1:1"})
                assert r.status_code == 403, (path, r.status_code)          # 本機但埠不同
                r = http.request(method, path, json=body, headers={"Host": "evil.example"})
                assert r.status_code == 403, (path, r.status_code)
                r = http.request(method, path, json=body, headers=same)
                assert r.status_code == 200, (path, r.status_code, r.text)
            assert db_snapshot(gw) == before   # GET 與 preview 都不寫入

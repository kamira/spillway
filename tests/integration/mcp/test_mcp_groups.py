"""群組名 `all` 保留(CHG-20261102-01 決策 22;AM-10)。

群組沒有改名端點(`PUT /api/groups/{name}` 只取代選擇器,路徑上的名稱不變),所以只有「建立」會撞名;
`PUT /api/groups/all` 因群組不存在回 404,也不會建立。
"""
from __future__ import annotations

import pytest

from spillway.store import repo

pytestmark = pytest.mark.anyio

SEL = {"all": ["a"], "any": [], "none": []}


async def test_create_group_named_all_is_422(asgi_app, db):
    r = await asgi_app.client.post("/api/groups", json={"name": "all", "selector": SEL})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_error"
    assert "body.name" in r.json()["error"]["message"]
    assert repo.get_group(db, "all") is None


async def test_put_group_all_does_not_create(asgi_app, db):
    r = await asgi_app.client.put("/api/groups/all", json={"selector": SEL})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert repo.get_group(db, "all") is None


@pytest.mark.parametrize("name", ["All", "ALL", "all2", "coding"])
async def test_other_names_still_allowed(asgi_app, name):
    r = await asgi_app.client.post("/api/groups", json={"name": name, "selector": SEL})
    assert r.status_code == 201, r.text

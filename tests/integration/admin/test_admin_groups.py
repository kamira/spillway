"""`/api/groups` CRUD 與成員預覽(CHG-20261030-02 T14)。"""
from __future__ import annotations

import pytest

from spillway.store import repo

from .helpers import api_member, assert_error, local_member

pytestmark = pytest.mark.anyio


async def seed(client):
    await client.post("/api/deployments", json=api_member("a", tags=["cloud", "fast"]))
    await client.post("/api/deployments", json=api_member("b", tags=["cloud"]))
    await client.post("/api/deployments", json=local_member("q", tags=["local", "fast"]))


async def test_group_crud_and_member_preview(client, db):
    await seed(client)
    r = await client.post("/api/groups", json={"name": "fast-cloud",
                                               "selector": {"all": ["cloud"], "any": ["fast"]}})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["name"] == "fast-cloud"
    assert out["selector"] == {"all": ["cloud"], "any": ["fast"], "none": []}
    assert [m["id"] for m in out["members"]] == ["a"]
    assert out["members"][0] == {"id": "a", "model_name": "a", "kind": "api",
                                 "provider": "openai", "enabled": True}

    got = (await client.get("/api/groups/fast-cloud")).json()
    assert got == out
    lst = (await client.get("/api/groups")).json()["groups"]
    assert [g["name"] for g in lst] == ["fast-cloud"] and lst[0]["members"] == out["members"]

    r = await client.put("/api/groups/fast-cloud", json={"selector": {"any": ["fast"],
                                                                      "none": ["local"]}})
    assert r.status_code == 200, r.text
    assert [m["id"] for m in r.json()["members"]] == ["a"]
    r = await client.put("/api/groups/fast-cloud", json={"selector": {"any": ["fast"]}})
    assert [m["id"] for m in r.json()["members"]] == ["a", "q"]

    assert (await client.delete("/api/groups/fast-cloud")).status_code == 204
    assert repo.get_group(db, "fast-cloud") is None
    assert_error(await client.get("/api/groups/fast-cloud"), 404, "not_found")


async def test_group_duplicate_and_missing(client):
    body = {"name": "g", "selector": {}}
    assert (await client.post("/api/groups", json=body)).status_code == 201
    assert_error(await client.post("/api/groups", json=body), 409, "conflict")
    assert_error(await client.put("/api/groups/zz", json={"selector": {}}), 404, "not_found")
    assert_error(await client.delete("/api/groups/zz"), 404, "not_found")


@pytest.mark.parametrize("selector,path", [
    ({"all": "cloud"}, "body.selector.all"),
    ({"any": [1]}, "body.selector.any"),
    ({"some": ["x"]}, "body.selector.some"),
])
async def test_group_selector_validation(client, selector, path):
    r = await client.post("/api/groups", json={"name": "g", "selector": selector})
    assert path in assert_error(r, 422, "validation_error")


async def test_delete_group_referenced_by_route_is_conflict(client, db):
    await client.post("/api/groups", json={"name": "g", "selector": {"all": ["cloud"]}})
    r = await client.post("/api/routes", json={"name": "rt", "target": {"group": "g"}})
    assert r.status_code == 201, r.text
    msg = assert_error(await client.delete("/api/groups/g"), 409, "conflict")
    assert "rt" in msg
    assert repo.get_group(db, "g") is not None
    await client.delete("/api/routes/rt")
    assert (await client.delete("/api/groups/g")).status_code == 204

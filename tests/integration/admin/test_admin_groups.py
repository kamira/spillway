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


# ---- CHG-20261102-02 T1:群組名稱規則(決策 11)

BAD_NAMES = [".", "..", "a\u202eb", "a\x00b", "a\u2028b", "a b", " ", "a/b", "all", "\ud800", "a\ue000b",
             "e\u0301", "x" * 201, "a\u200bb", "a\u00a0b", "a\u0378b"]
GOOD_NAMES = ["coding", "中文群組", "a.b", "v1-2", "...", "x" * 200]


@pytest.mark.parametrize("name", BAD_NAMES, ids=[ascii(n)[:30] for n in BAD_NAMES])
async def test_bad_group_names_are_422(client, name):
    import json
    # ensure_ascii=True:孤立代理字元以 \ud800 跳脫送出(httpx 的 json= 會因無法 UTF-8 編碼而拒絕)
    raw = json.dumps({"name": name, "selector": {}}).encode("ascii")
    r = await client.post("/api/groups", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("name", GOOD_NAMES)
async def test_good_group_names_are_201(client, name):
    r = await client.post("/api/groups", json={"name": name, "selector": {}})
    assert r.status_code == 201, r.text


def test_group_name_problem_pure():
    from spillway.api.admin.models import group_name_problem
    assert all(group_name_problem(n) for n in BAD_NAMES + [""])
    assert all(group_name_problem(n) is None for n in GOOD_NAMES)


async def test_existing_hostile_group_names_still_usable(client, db):
    from urllib.parse import quote
    names = ["a\u202eb", "a\u2028b"]
    for n in names:
        repo.save_group(db, repo.Group(n, {"all": ["x"], "any": [], "none": []}))
    for n in names:
        p = "/api/groups/" + quote(n, safe="")
        assert (await client.get(p)).status_code == 200
        assert (await client.put(p, json={"selector": {"any": ["y"]}})).status_code == 200
        assert (await client.delete(p)).status_code in (200, 204)
        assert (await client.get(p)).status_code == 404


async def test_member_id_rule_unchanged(client):
    r = await client.post("/api/deployments", json=api_member("a.b", tags=[]))
    assert r.status_code in (200, 201), r.text


# ================================================================ POST /api/groups/preview
# CHG-20261103-01 T2(決策 10 ①;AM-13)


async def test_preview_matches_saved_group_members(client):
    await seed(client)
    await client.post("/api/deployments", json=api_member("c-off", tags=["coding"], enabled=False))
    await client.post("/api/deployments", json=api_member("b2", tags=["coding", "cloud"]))
    sel = {"any": ["coding"]}
    r = await client.post("/api/groups/preview", json={"selector": sel})
    assert r.status_code == 200, r.text
    preview = r.json()
    assert set(preview) == {"members"}
    assert [m["id"] for m in preview["members"]] == ["b2", "c-off"]       # 含停用、依 id 排序
    assert preview["members"][1]["enabled"] is False
    assert (await client.post("/api/groups", json={"name": "coding", "selector": sel})).status_code == 201
    assert (await client.get("/api/groups/coding")).json()["members"] == preview["members"]


async def test_preview_empty_selector_matches_all(client):
    await seed(client)
    await client.post("/api/deployments", json=api_member("z-off", tags=[], enabled=False))
    for body in ({"selector": {}}, {"selector": {"all": [], "any": [], "none": []}}):
        r = await client.post("/api/groups/preview", json=body)
        assert r.status_code == 200, r.text
        assert [m["id"] for m in r.json()["members"]] == ["a", "b", "q", "z-off"]


@pytest.mark.parametrize("body", [
    {"selector": {"some": ["x"]}},          # 未知選擇器鍵
    {"selector": {"any": [1]}},             # 非字串 tag
    {"selector": {"any": "coding"}},
    {"selector": {}, "name": "x"},          # body 未知鍵
    {},
])
async def test_preview_validation(client, body):
    assert_error(await client.post("/api/groups/preview", json=body), 422, "validation_error")


async def test_preview_does_not_write(client, db):
    await seed(client)
    before = list(db.iterdump())
    r = await client.post("/api/groups/preview", json={"selector": {"all": ["cloud"]}})
    assert r.status_code == 200
    assert list(db.iterdump()) == before
    assert repo.list_groups(db) == []


async def test_preview_has_no_token_or_hash(client, db):
    from spillway import settings
    await seed(client)
    token = settings.rotate_access_token(db)
    digest = repo.get_setting(db, settings.ACCESS_TOKEN_KEY)
    assert token and digest
    r = await client.post("/api/groups/preview", json={"selector": {}})
    assert r.status_code == 200
    assert token not in r.text and digest not in r.text

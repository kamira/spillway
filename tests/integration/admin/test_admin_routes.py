"""`/api/routes` CRUD、order(9.3、4.3.4)、dry-run(Plan)、`/api/estimate`(CHG-20261030-02 T14)。"""
from __future__ import annotations

import pytest

from spillway.store import repo

from .helpers import api_member, assert_error, local_member

pytestmark = pytest.mark.anyio

REQ = {"messages": [{"role": "user", "content": "hello there"}]}


async def seed(client, fake_api):
    for body in (api_member("a", fake_api=fake_api), api_member("b", fake_api=fake_api),
                 local_member("q", fake_api=fake_api),
                 api_member("x", tags=["other"], fake_api=fake_api)):
        r = await client.post("/api/deployments", json=body)
        assert r.status_code == 201, r.text
    r = await client.post("/api/routes", json={"name": "rt", "target": {"tags": {"all": ["r"]}}})
    assert r.status_code == 201, r.text


def slot_ids(plan: dict) -> list[tuple[int | None, list[str]]]:
    return [(s["number"], sorted(e["deployment"] for e in s["entries"])) for s in plan["slots"]]


# ---------------------------------------------------------------- CRUD


async def test_route_crud(client, db):
    body = {"name": "rt", "target": {"tags": {"all": ["r"], "none": ["slow"]}}, "mode": "task",
            "tags": ["t1"], "record_context": False, "budget_precheck": True}
    r = await client.post("/api/routes", json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out == {"name": "rt", "target": {"tags": {"all": ["r"], "any": [], "none": ["slow"]}},
                   "mode": "task", "tags": ["t1"], "record_context": False,
                   "budget_precheck": True, "order": []}
    assert (await client.get("/api/routes/rt")).json() == out
    assert [x["name"] for x in (await client.get("/api/routes")).json()["routes"]] == ["rt"]

    r = await client.put("/api/routes/rt", json={"target": {"tags": {"any": ["r"]}}})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["mode"] == "chat" and out["record_context"] is None
    assert out["budget_precheck"] is False
    assert repo.get_route(db, "rt").target == {"tags": {"all": [], "any": ["r"], "none": []}}

    assert (await client.delete("/api/routes/rt")).status_code == 204
    assert_error(await client.get("/api/routes/rt"), 404, "not_found")
    assert_error(await client.delete("/api/routes/rt"), 404, "not_found")


async def test_route_duplicate_conflict(client):
    body = {"name": "rt", "target": {"tags": {}}}
    assert (await client.post("/api/routes", json=body)).status_code == 201
    assert_error(await client.post("/api/routes", json=body), 409, "conflict")


async def test_route_group_target(client):
    assert_error(await client.post("/api/routes", json={"name": "rt", "target": {"group": "g"}}),
                 422, "validation_error")
    await client.post("/api/groups", json={"name": "g", "selector": {}})
    r = await client.post("/api/routes", json={"name": "rt", "target": {"group": "g"}})
    assert r.status_code == 201 and r.json()["target"] == {"group": "g"}


@pytest.mark.parametrize("body,path", [
    ({"name": "rt", "target": {"group": "nope"}}, "body.target.group"),
    ({"name": "rt", "target": {"tags": {"all": "r"}}}, "body.target"),
    ({"name": "rt", "target": {"group": "g", "tags": {}}}, "body.target"),
    ({"name": "rt", "target": {}}, "body.target"),
    ({"name": "rt", "target": {"tags": {}}, "mode": "batch"}, "body.mode"),
    ({"name": "rt", "target": {"tags": {}}, "record_context": "yes"}, "body.record_context"),
    ({"name": "rt", "target": {"tags": {}}, "budget_precheck": 3}, "body.budget_precheck"),
    ({"name": "a/b", "target": {"tags": {}}}, "body.name"),
])
async def test_route_validation(client, body, path):
    msg = assert_error(await client.post("/api/routes", json=body), 422, "validation_error")
    assert path in msg


# ---------------------------------------------------------------- order


async def test_put_order_changes_dry_run_then_delete_restores_default(client, db, fake_api):
    await seed(client, fake_api)
    default = (await client.post("/api/routes/rt/dry-run", json=REQ)).json()
    assert slot_ids(default) == [(2, ["a", "b"]), (3, ["q"])]   # 4.3.2:api slot、local 各一

    order = {"slots": [[{"deployment": "q"}],
                       [{"deployment": "b", "effort": "high", "weight": "high"}],
                       [{"deployment": "a", "effort": "low", "weight": "low"}]]}
    r = await client.put("/api/routes/rt/order", json=order)
    assert r.status_code == 200, r.text
    assert r.json()["order"] == [
        [{"deployment": "q", "effort": None, "weight": "medium"}],
        [{"deployment": "b", "effort": "high", "weight": "high"}],
        [{"deployment": "a", "effort": "low", "weight": "low"}],
    ]
    rows = repo.list_route_targets(db, "rt")
    assert [(t.deployment_id, t.slot, t.position) for t in rows] == [
        ("q", 1, 0), ("b", 2, 0), ("a", 3, 0)]

    plan = (await client.post("/api/routes/rt/dry-run", json=REQ)).json()
    assert slot_ids(plan) == [(1, ["q"]), (2, ["b"]), (3, ["a"])]
    assert plan["first"] == "q"
    b = plan["slots"][1]["entries"][0]
    assert b["weight"] == "high" and b["effort"] == "high"

    assert (await client.delete("/api/routes/rt/order")).status_code == 204
    assert repo.list_route_targets(db, "rt") == []
    assert slot_ids((await client.post("/api/routes/rt/dry-run", json=REQ)).json()) == \
        slot_ids(default)


async def test_put_order_member_not_in_target_keeps_old(client, db, fake_api):
    await seed(client, fake_api)
    await client.put("/api/routes/rt/order", json={"slots": [[{"deployment": "a"}]]})
    for dep in ("x", "ghost"):
        r = await client.put("/api/routes/rt/order",
                             json={"slots": [[{"deployment": "b"}], [{"deployment": dep}]]})
        msg = assert_error(r, 422, "member_not_in_target")
        assert dep in msg
    assert [t.deployment_id for t in repo.list_route_targets(db, "rt")] == ["a"]


@pytest.mark.parametrize("order,path", [
    ({"slots": [[{"deployment": "a", "effort": "ultra"}]]}, "body.slots.0.0.effort"),
    ({"slots": [[{"deployment": "a", "weight": "huge"}]]}, "body.slots.0.0.weight"),
    ({"slots": [[{"deployment": "a"}], [{"deployment": "a"}]]}, "body.slots"),
    ({"slots": [[]]}, "body.slots.0"),
    ({}, "body.slots"),
])
async def test_put_order_validation(client, fake_api, order, path):
    await seed(client, fake_api)
    msg = assert_error(await client.put("/api/routes/rt/order", json=order),
                       422, "validation_error")
    assert path in msg


async def test_order_on_missing_route_404(client):
    assert_error(await client.put("/api/routes/zz/order", json={"slots": []}), 404, "not_found")
    assert_error(await client.delete("/api/routes/zz/order"), 404, "not_found")


# ---------------------------------------------------------------- dry-run 與 estimate


async def test_dry_run_plan_json_shape_and_no_ledger(client, db, fake_api):
    await seed(client, fake_api)
    await client.put("/api/deployments/b", json={**api_member("b", fake_api=fake_api),
                                                 "enabled": False})
    await client.put("/api/routes/rt/order", json={"slots": [[{"deployment": "a"}]]})
    r = await client.post("/api/routes/rt/dry-run", json={**REQ, "max_tokens": 300})
    assert r.status_code == 200, r.text
    plan = r.json()
    assert set(plan) == {"route_name", "slots", "excluded", "unsorted", "first"}
    assert plan["route_name"] == "rt" and plan["first"] == "a"
    entry = plan["slots"][0]["entries"][0]
    assert set(entry) == {"deployment", "kind", "weight", "effort", "position", "unsorted",
                          "health", "estimate"}
    assert entry["estimate"]["out_p50"] == entry["estimate"]["out_p90"] == 300
    assert set(entry["estimate"]) == {"tokens_in", "out_p50", "out_p90", "cost_usd_p90"}
    assert plan["unsorted"] == ["q"]
    q = plan["slots"][1]["entries"][0]
    assert q["deployment"] == "q" and q["unsorted"] is True and q["health"] == "health_unknown"
    assert plan["excluded"] == [{"deployment": "b", "reason": "disabled", "estimate": None}]
    assert repo.list_ledger(db) == [] and fake_api.requests == []


async def test_dry_run_skipped_precheck_has_estimate(client, fake_api):
    await client.post("/api/deployments", json=api_member(
        "a", fake_api=fake_api, budget={"type": "usd", "limit_amount": 1e-9}))
    await client.post("/api/routes", json={"name": "rt", "target": {"tags": {"all": ["r"]}},
                                           "budget_precheck": True})
    plan = (await client.post("/api/routes/rt/dry-run", json=REQ)).json()
    (ex,) = plan["excluded"]
    assert ex["deployment"] == "a" and ex["reason"] == "skipped_precheck"
    assert ex["estimate"]["cost_usd_p90"] > 0


async def test_dry_run_missing_route_and_bad_body(client, fake_api):
    assert_error(await client.post("/api/routes/zz/dry-run", json=REQ), 404, "not_found")
    await seed(client, fake_api)
    msg = assert_error(await client.post("/api/routes/rt/dry-run", json={}), 422,
                       "validation_error")
    assert "body.messages" in msg
    r = await client.post("/api/routes/rt/dry-run", json={**REQ, "reasoning_effort": "ultra"})
    assert "reasoning_effort" in assert_error(r, 422, "validation_error")


async def test_estimate_lists_candidates(client, db, fake_api):
    await seed(client, fake_api)
    r = await client.post("/api/estimate", json={"model": "rt", **REQ, "max_tokens": 120})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["route_name"] == "rt"
    got = {c["deployment"]: c for c in out["candidates"]}
    assert set(got) == {"a", "b", "q"}
    assert got["a"]["slot"] == 2 and got["q"]["slot"] == 3
    for c in got.values():
        assert c["estimate"]["out_p90"] == 120 and c["estimate"]["tokens_in"] > 0
    assert got["q"]["estimate"]["cost_usd_p90"] is None          # local 無定價
    assert got["a"]["estimate"]["cost_usd_p90"] is not None
    assert out["excluded"] == []
    assert repo.list_ledger(db) == [] and fake_api.requests == []


async def test_estimate_local_direct_and_errors(client, fake_api):
    await seed(client, fake_api)
    out = (await client.post("/api/estimate", json={"model": "local/q", **REQ})).json()
    assert out["route_name"] is None
    assert [c["deployment"] for c in out["candidates"]] == ["q"]
    assert out["candidates"][0]["slot"] is None
    assert out["candidates"][0]["estimate"]["out_p50"] == 500        # 帳本不足 5 筆
    assert_error(await client.post("/api/estimate", json={"model": "nope", **REQ}),
                 404, "not_found")
    assert "body.model" in assert_error(await client.post("/api/estimate", json=REQ),
                                        422, "validation_error")

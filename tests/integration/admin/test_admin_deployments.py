"""`/api/deployments` CRUD、寫入驗證、cardinality、test、clear-cooldown(CHG-20261030-02 T14)。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from spillway.store import repo
from tests.contract.fakemods import write_module

from .helpers import KEY, api_member, assert_error, local_member

pytestmark = pytest.mark.anyio


def count(db, sql: str, *args) -> int:
    return db.execute(sql, args).fetchone()[0]


# ---------------------------------------------------------------- CRUD


async def test_create_get_list_roundtrip_with_tags_and_budget(client, db):
    body = api_member("a", tags=["r", "fast"],
                      budget={"type": "usd", "limit_amount": 10.0, "window": "monthly",
                              "reset_anchor": 5})
    r = await client.post("/api/deployments", json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["id"] == "a" and out["tags"] == ["fast", "r"]
    assert out["budget"] == {"type": "usd", "limit_amount": 10.0, "window": "monthly",
                             "reset_anchor": 5, "exhausted_until": None}
    assert out["credential_ref"] == "env:SPILLWAY_TEST_KEY"

    got = (await client.get("/api/deployments/a")).json()
    assert got == out
    lst = (await client.get("/api/deployments")).json()
    assert [d["id"] for d in lst["deployments"]] == ["a"]
    dep = repo.load_deployment(db, "a")
    assert dep.tags == ("fast", "r") and dep.budget.limit_amount == 10.0


async def test_responses_never_contain_credential_value(client):
    await client.post("/api/deployments", json=api_member("a"))
    for path in ("/api/deployments", "/api/deployments/a"):
        r = await client.get(path)
        assert KEY not in r.text
        assert "env:SPILLWAY_TEST_KEY" in r.text


async def test_put_replaces_fields_and_keeps_cooldown(client, db, clock):
    await client.post("/api/deployments", json=api_member(
        "a", budget={"type": "usd", "limit_amount": 5, "window": "monthly", "reset_anchor": 1}))
    until = clock.now() + timedelta(hours=1)
    repo.set_exhausted_until(db, "a", until)
    body = api_member("a", tags=["x"], model="gpt-4.1-mini",
                      budget={"type": "usd", "limit_amount": 7, "window": "daily"})
    body.pop("id")
    r = await client.put("/api/deployments/a", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["model"] == "gpt-4.1-mini" and out["tags"] == ["x"]
    assert out["budget"]["limit_amount"] == 7 and out["budget"]["window"] == "daily"
    assert out["budget"]["exhausted_until"] == "2026-10-30T13:00:00Z"   # 編輯不清掉冷卻


async def test_put_id_mismatch_is_422(client):
    await client.post("/api/deployments", json=api_member("a"))
    r = await client.put("/api/deployments/a", json=api_member("b"))
    msg = assert_error(r, 422, "validation_error")
    assert "body.id" in msg


async def test_delete_cascades_and_keeps_ledger(client, db, clock):
    await client.post("/api/deployments", json=api_member("a"))
    repo.insert_ledger(db, repo.LedgerRow(ts=clock.now(), status="ok", deployment_id="a"))
    r = await client.delete("/api/deployments/a")
    assert r.status_code == 204
    assert count(db, "SELECT COUNT(*) FROM deployments") == 0
    assert count(db, "SELECT COUNT(*) FROM deployment_tags") == 0
    assert count(db, "SELECT COUNT(*) FROM usage_ledger") == 1
    assert_error(await client.delete("/api/deployments/a"), 404, "not_found")


async def test_missing_member_404(client):
    assert_error(await client.get("/api/deployments/nope"), 404, "not_found")
    body = api_member("nope")
    body.pop("id")
    assert_error(await client.put("/api/deployments/nope", json=body), 404, "not_found")
    assert_error(await client.post("/api/deployments/nope/clear-cooldown"), 404, "not_found")
    assert_error(await client.post("/api/deployments/nope/test"), 404, "not_found")


async def test_duplicate_id_is_conflict(client, db):
    await client.post("/api/deployments", json=api_member("a"))
    assert_error(await client.post("/api/deployments", json=api_member("a")), 409, "conflict")
    assert count(db, "SELECT COUNT(*) FROM deployments") == 1


# ---------------------------------------------------------------- 寫入驗證(決策 22)


async def test_credential_ref_raw_key_rejected_without_echo(client, db):
    r = await client.post("/api/deployments", json=api_member("a", credential_ref="sk-abc"))
    msg = assert_error(r, 422, "validation_error")
    assert "credential_ref" in msg
    assert "sk-abc" not in r.text
    assert count(db, "SELECT COUNT(*) FROM deployments") == 0


@pytest.mark.parametrize("ref", ["env:SPILLWAY_TEST_KEY", "keyring:spillway:acct"])
async def test_credential_ref_valid_forms(client, ref):
    r = await client.post("/api/deployments", json=api_member("a", credential_ref=ref))
    assert r.status_code == 201, r.text


async def test_params_with_key_rejected_without_echo(client, db):
    r = await client.post("/api/deployments", json=api_member(
        "a", params={"headers": {"x-key": f"prefix {KEY}"}}))
    msg = assert_error(r, 422, "validation_error")
    assert "body.params" in msg
    assert KEY not in r.text
    r = await client.post("/api/deployments", json=api_member(
        "a", params={"list": ["ok", "Bearer abcdefgh"]}))
    assert_error(r, 422, "validation_error")
    assert "abcdefgh" not in r.text
    assert count(db, "SELECT COUNT(*) FROM deployments") == 0


async def test_put_params_with_key_rejected(client, db):
    await client.post("/api/deployments", json=api_member("a"))
    body = api_member("a", params={"k": KEY})
    r = await client.put("/api/deployments/a", json=body)
    assert_error(r, 422, "validation_error")
    assert KEY not in r.text
    assert repo.load_deployment(db, "a").params == {}


@pytest.mark.parametrize("anchor", [0, 29])
async def test_reset_anchor_out_of_range(client, anchor):
    r = await client.post("/api/deployments", json=api_member(
        "a", budget={"type": "usd", "limit_amount": 1, "window": "monthly",
                     "reset_anchor": anchor}))
    msg = assert_error(r, 422, "validation_error")
    assert "body.budget.reset_anchor" in msg


@pytest.mark.parametrize("patch,path", [
    ({"kind": "cloud"}, "body.kind"),
    ({"capabilities": {"efforts": ["low", "ultra"]}}, "body.capabilities.efforts"),
    ({"budget": {"type": "gold", "limit_amount": 1}}, "body.budget.type"),
    ({"budget": {"type": "usd", "limit_amount": 1, "window": "yearly"}}, "body.budget.window"),
    ({"model": ""}, "body.model"),
    ({"tags": ["ok", ""]}, "body.tags"),
    ({"unknown_field": 1}, "body.unknown_field"),
])
async def test_field_validation_reports_path(client, patch, path):
    r = await client.post("/api/deployments", json=api_member("a", **patch))
    msg = assert_error(r, 422, "validation_error")
    assert path in msg


async def test_missing_required_field(client):
    body = api_member("a")
    body.pop("provider")
    msg = assert_error(await client.post("/api/deployments", json=body), 422, "validation_error")
    assert "body.provider" in msg


# ---------------------------------------------------------------- cardinality(7.6.5、決策 22)


async def test_multiple_provider_allows_second_member(client, db):
    assert (await client.post("/api/deployments", json=api_member("a"))).status_code == 201
    assert (await client.post("/api/deployments", json=api_member("b"))).status_code == 201
    assert count(db, "SELECT COUNT(*) FROM deployments WHERE provider='openai'") == 2


@pytest.fixture
def single_app(tmp_path, make_app):
    root = tmp_path / "mods"
    write_module(root, "solo", providers=("solo-cli",), kinds=("cli",),
                 access=("subscription-cli",), cardinality="single")
    return make_app(extra_paths=[root])


def solo(dep_id: str) -> dict:
    return {"id": dep_id, "model_name": dep_id, "kind": "cli", "provider": "solo-cli",
            "model": "fake-small", "tags": ["r"]}


async def test_single_provider_second_member_409(single_app, make_client, db):
    async with make_client(single_app) as c:
        assert (await c.post("/api/deployments", json=solo("s1"))).status_code == 201
        r = await c.post("/api/deployments", json=solo("s2"))
        assert_error(r, 409, "provider_single")
    assert count(db, "SELECT COUNT(*) FROM deployments WHERE provider='solo-cli'") == 1
    assert count(db, "SELECT COUNT(*) FROM deployments") == 1


async def test_single_provider_put_change_provider_409(single_app, make_client, db):
    async with make_client(single_app) as c:
        assert (await c.post("/api/deployments", json=solo("s1"))).status_code == 201
        assert (await c.post("/api/deployments", json=api_member("a"))).status_code == 201
        body = solo("a")
        r = await c.put("/api/deployments/a", json=body)
        assert_error(r, 409, "provider_single")
        # 自己原本就是該 provider 的唯一成員:PUT 不算第二個
        r = await c.put("/api/deployments/s1", json={**solo("s1"), "model": "fake-large"})
        assert r.status_code == 200, r.text
    assert repo.load_deployment(db, "a").provider == "openai"
    assert count(db, "SELECT COUNT(*) FROM deployments WHERE provider='solo-cli'") == 1


# ---------------------------------------------------------------- clear-cooldown、test


async def test_clear_cooldown(client, db, clock):
    await client.post("/api/deployments", json=api_member(
        "a", budget={"type": "usd", "limit_amount": 5}))
    repo.set_exhausted_until(db, "a", clock.now() + timedelta(hours=1))
    r = await client.post("/api/deployments/a/clear-cooldown")
    assert r.status_code == 200, r.text
    assert r.json()["budget"]["exhausted_until"] is None
    assert repo.get_budget(db, "a").exhausted_until is None


async def test_test_call_ok_writes_ledger(client, db, fake_api):
    await client.post("/api/deployments", json=api_member("a", fake_api=fake_api))
    r = await client.post("/api/deployments/a/test")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["deployment"] == "a" and out["status"] == "ok" and out["ok"] is True
    assert out["text"] == "fake upstream ok" and out["error"] is None
    assert out["tokens_in"] == 11 and out["tokens_out"] == 7
    assert isinstance(out["latency_ms"], int)
    rows = repo.list_ledger(db)
    assert len(rows) == 1 and rows[0].id == out["ledger_id"]
    assert rows[0].deployment_id == "a" and rows[0].route_name is None and rows[0].slot is None
    assert rows[0].est_tokens_in is not None
    assert len(fake_api.requests) == 1


async def test_test_call_rate_limited_sets_cooldown_and_hides_key(client, db, fake_api, clock):
    await client.post("/api/deployments", json=api_member("a", fake_api=fake_api))
    fake_api.set_mode("*", "http_429")
    out = (await client.post("/api/deployments/a/test")).json()
    assert out["status"] == "rate_limited" and out["ok"] is False
    assert repo.get_budget(db, "a").exhausted_until == clock.now() + timedelta(seconds=60)
    fake_api.set_mode("*", "echo_auth")
    r = await client.post("/api/deployments/a/test")
    assert r.json()["status"] == "error"
    assert KEY not in r.text
    assert all(KEY not in (row.error or "") for row in repo.list_ledger(db))


async def test_test_call_adapter_unavailable(tmp_path, make_app, make_client, db):
    root = tmp_path / "mods"
    write_module(root, "broken", pre_import="import not_a_real_pkg_xyz\n",
                 providers=("broken-p",))
    async with make_client(make_app(extra_paths=[root])) as c:
        await c.post("/api/deployments", json=api_member("b", provider="broken-p"))
        out = (await c.post("/api/deployments/b/test")).json()
    assert out["status"] == "adapter_unavailable" and out["ledger_id"] is None
    assert repo.list_ledger(db) == []


async def test_local_member_create(client):
    r = await client.post("/api/deployments", json=local_member("q"))
    assert r.status_code == 201, r.text
    assert r.json()["credential_ref"] is None

"""api.admin 錯誤碼一律為 spec 9.1.2 格式(契約清單「api.admin 錯誤碼」;決策 16、17、22)。

每個錯誤碼至少一個情境:`validation_error`、`not_found`、`conflict`、`provider_single`、
`member_not_in_target`、`payload_expired`。
"""
from __future__ import annotations

import pytest

from spillway.ledger import Attempt, LedgerWriter
from tests.contract.fakemods import write_module

from .helpers import api_member, assert_error

pytestmark = pytest.mark.anyio


async def test_validation_error_format_and_field_path(client):
    r = await client.post("/api/deployments", json={"id": "a"})
    msg = assert_error(r, 422, "validation_error")
    for field in ("body.model_name", "body.kind", "body.provider", "body.model"):
        assert field in msg
    assert "detail" not in r.json()


async def test_validation_error_invalid_json(client):
    r = await client.post("/api/deployments", content=b"{not json",
                          headers={"content-type": "application/json"})
    assert_error(r, 422, "validation_error")


async def test_validation_error_does_not_echo_input(client):
    r = await client.post("/api/deployments", json=api_member("a", kind="sk-test-0000"))
    assert_error(r, 422, "validation_error")
    assert "sk-test-0000" not in r.text


async def test_not_found(client):
    for method, path in (("GET", "/api/deployments/x"), ("GET", "/api/groups/x"),
                         ("GET", "/api/routes/x"), ("GET", "/api/ledger/1/payload")):
        assert_error(await client.request(method, path), 404, "not_found")


async def test_conflict(client):
    await client.post("/api/deployments", json=api_member("a"))
    assert_error(await client.post("/api/deployments", json=api_member("a")), 409, "conflict")


async def test_provider_single(tmp_path, make_app, make_client):
    root = tmp_path / "mods"
    write_module(root, "solo", providers=("solo-cli",), kinds=("cli",),
                 access=("subscription-cli",), cardinality="single")
    body = {"model_name": "s", "kind": "cli", "provider": "solo-cli", "model": "m"}
    async with make_client(make_app(extra_paths=[root])) as c:
        await c.post("/api/deployments", json={"id": "s1", **body})
        msg = assert_error(await c.post("/api/deployments", json={"id": "s2", **body}),
                           409, "provider_single")
    assert "solo-cli" in msg


async def test_member_not_in_target(client):
    await client.post("/api/deployments", json=api_member("a", tags=["x"]))
    await client.post("/api/routes", json={"name": "rt", "target": {"tags": {"all": ["r"]}}})
    r = await client.put("/api/routes/rt/order", json={"slots": [[{"deployment": "a"}]]})
    assert_error(r, 422, "member_not_in_target")


async def test_payload_expired(client, db, clock):
    lid = LedgerWriter(db).record(Attempt(ts=clock.now(), status="ok", deployment_id="a",
                                          request_text="[]", response_text="x"))
    clock.advance(days=30)
    assert_error(await client.get(f"/api/ledger/{lid}/payload"), 410, "payload_expired")

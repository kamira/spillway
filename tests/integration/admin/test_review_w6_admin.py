"""T14 審議回修(CHG-20261030-02 波次 6:M23、M24、M25、U9 與建議修)。"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from spillway.adapters.base import AdapterUnavailableError
from spillway.api import admin
from spillway.domain import CallResult
from spillway.store import repo
from tests.contract.fakemods import write_module

from .helpers import api_member, assert_error

pytestmark = pytest.mark.anyio

EXTRA_SECRET = "zq9extrasecretvalue77"     # 非 sk- 格式:只能靠 extra_secrets 遮罩


# ---------------------------------------------------------------- M23 單一錯誤工具


def test_admin_uses_shared_error_tools():
    import spillway.api as api

    assert admin.__all__ == ["router"]
    assert admin.ApiError is api.ApiError
    for name in ("_error_response", "_format_validation"):
        assert not hasattr(admin, name)


async def test_admin_validation_empty_loc_and_format(client):
    r = await client.post("/api/deployments", content=b"[1]",
                          headers={"content-type": "application/json"})
    assert_error(r, 422, "validation_error")


# ---------------------------------------------------------------- M24 /test 與決策 35 對齊


class _FakeAdapter:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = 0

    async def call(self, dep, req, effort):
        self.calls += 1
        return await self.behaviour()


def _patch_adapter(app, behaviour) -> _FakeAdapter:
    fake = _FakeAdapter(behaviour)
    app.state.registry.get = lambda dep: fake
    return fake


async def test_mark_failure_still_writes_ledger(make_app, make_client, db, monkeypatch, caplog):
    app = make_app()

    async def limited():
        return CallResult(ok=False, text="", tokens_in=None, tokens_out=None, cost_usd=None,
                          status="rate_limited", error="429")

    def boom(self, dep, status):
        raise RuntimeError("mark exploded sk-test-0000")

    monkeypatch.setattr(admin.QuotaService, "mark", boom)
    async with make_client(app) as c:
        await c.post("/api/deployments", json=api_member("a"))
        _patch_adapter(app, limited)
        with caplog.at_level(logging.WARNING):
            r = await c.post("/api/deployments/a/test")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "rate_limited" and out["ledger_id"] is not None
    rows = repo.list_ledger(db)
    assert len(rows) == 1 and rows[0].status == "rate_limited"
    assert "RuntimeError" in caplog.text
    assert "mark exploded" not in caplog.text


async def test_cancelled_call_writes_cancelled_row_then_reraises(make_app, make_client, db):
    app = make_app()

    async def cancelled():
        raise asyncio.CancelledError

    async with make_client(app) as c:
        await c.post("/api/deployments", json=api_member("a"))
    _patch_adapter(app, cancelled)
    request = SimpleNamespace(app=app)
    with pytest.raises(asyncio.CancelledError):
        await admin.test_deployment(request, "a")
    rows = repo.list_ledger(db)
    assert len(rows) == 1
    assert rows[0].status == "error" and rows[0].error == "cancelled"
    assert rows[0].deployment_id == "a" and rows[0].route_name is None


async def test_text_redacted_before_truncation(make_app, make_client, db, monkeypatch):
    monkeypatch.setenv("W6_EXTRA", EXTRA_SECRET)
    app = make_app()
    text = "a" * 195 + EXTRA_SECRET + " tail"          # 金鑰橫跨第 200 字元

    async def ok():
        return CallResult(ok=True, text=text, tokens_in=1, tokens_out=1, cost_usd=0.0,
                          status="ok", error=None)

    async with make_client(app) as c:
        await c.post("/api/deployments", json=api_member("x", credential_ref="env:W6_EXTRA"))
        _patch_adapter(app, ok)
        r = await c.post("/api/deployments/x/test")
    assert r.status_code == 200, r.text
    out = r.json()
    assert len(out["text"]) <= admin.TEST_TEXT_PREVIEW
    for i in range(4, len(EXTRA_SECRET) + 1):         # 不殘留任何前綴片段
        assert EXTRA_SECRET[:i] not in out["text"]
    assert out["text"].startswith("a" * 195 + "[RED")


async def test_adapter_unavailable_reason_is_redacted(make_app, make_client):
    app = make_app()

    def unavailable(dep):
        raise AdapterUnavailableError(dep.provider, "import 失敗:token sk-test-0000abcd")

    async with make_client(app) as c:
        await c.post("/api/deployments", json=api_member("a"))
        app.state.registry.get = unavailable
        out = (await c.post("/api/deployments/a/test")).json()
    assert out["status"] == "adapter_unavailable"
    assert "sk-test-0000" not in out["error"]


# ---------------------------------------------------------------- M25 保存天數上限


@pytest.mark.parametrize("days,status", [(36500, 200), (36501, 422), (10**6, 422)])
async def test_context_retention_days_upper_bound(client, days, status):
    r = await client.put("/api/settings/context", json={"context_retention_days": days})
    if status == 422:
        assert_error(r, 422, "validation_error")
    else:
        assert r.status_code == 200, r.text
        assert r.json()["context_retention_days"] == days


# ---------------------------------------------------------------- U9 憑證鍵名


@pytest.mark.parametrize("params", [
    {"api_key": EXTRA_SECRET},
    {"API_KEY": EXTRA_SECRET},
    {"ApiKey": EXTRA_SECRET},
    {"Authorization": EXTRA_SECRET},
    {"nested": {"deeper": {"Token": EXTRA_SECRET}}},
    {"list": [{"password": EXTRA_SECRET}]},
    {"secret": 12345},
    {"access_token": EXTRA_SECRET},
    {"key": EXTRA_SECRET},
])
async def test_params_credential_key_names_rejected_without_echo(client, db, params):
    r = await client.post("/api/deployments", json=api_member("a", params=params))
    msg = assert_error(r, 422, "validation_error")
    assert EXTRA_SECRET not in r.text and "12345" not in r.text
    assert "credential_ref" in msg
    assert repo.load_deployment(db, "a") is None


async def test_capabilities_extra_credential_key_rejected(client, db):
    caps = {"ctx": 1000, "tools": True, "vision": False, "efforts": [],
            "extra": {"Secret": EXTRA_SECRET}}
    r = await client.post("/api/deployments", json=api_member("a", capabilities=caps))
    assert_error(r, 422, "validation_error")
    assert EXTRA_SECRET not in r.text
    caps = {"ctx": 1000, "PASSWORD": EXTRA_SECRET}
    r = await client.post("/api/deployments", json=api_member("a", capabilities=caps))
    assert_error(r, 422, "validation_error")
    assert EXTRA_SECRET not in r.text


@pytest.mark.parametrize("params", [
    {"max_tokens": 10},
    {"api_base": "http://127.0.0.1:9/v1", "timeout": 30},
    {"token_budget": 5, "keys": ["a"], "secret_sauce": "x", "monkey": 1},
])
async def test_similar_key_names_are_not_blocked(client, params):
    r = await client.post("/api/deployments", json=api_member("a", params=params))
    assert r.status_code == 201, r.text
    assert r.json()["params"] == params


async def test_put_params_credential_key_rejected(client, db):
    assert (await client.post("/api/deployments", json=api_member("a"))).status_code == 201
    r = await client.put("/api/deployments/a", json=api_member("a", params={"Api_Key": "v1v1v1"}))
    assert_error(r, 422, "validation_error")
    assert "v1v1v1" not in r.text


# ---------------------------------------------------------------- 建議修


@pytest.fixture
def cli_fallback_single_app(tmp_path, make_app):
    """未宣告的 provider:kind cli 落到 single 的後備模組,kind api 落到內建 litellm(multiple)。"""
    root = tmp_path / "mods"
    write_module(root, "clifb", providers=("clifb-own",), kinds=("cli",),
                 access=("subscription-cli",), cardinality="single", fallback_for_kinds=("cli",))
    return make_app(extra_paths=[root])


async def test_put_changing_kind_checks_cardinality(cli_fallback_single_app, make_client, db):
    base = {"model_name": "m", "provider": "undeclared-p", "model": "fake-small", "tags": ["r"]}
    async with make_client(cli_fallback_single_app) as c:
        r = await c.post("/api/deployments", json={**base, "id": "s1", "kind": "cli"})
        assert r.status_code == 201, r.text
        r = await c.post("/api/deployments", json={**base, "id": "s2", "kind": "api"})
        assert r.status_code == 201, r.text           # api 走 multiple 的後備模組
        # 只改 kind(provider 不變):改成 cli 後落到 single 模組,已有 s1 → 409
        r = await c.put("/api/deployments/s2", json={**base, "kind": "cli"})
        assert_error(r, 409, "provider_single")
    assert repo.load_deployment(db, "s2").kind == "api"


async def test_not_found_messages_have_space_and_redact(client):
    r = await client.get("/api/routes/nope")
    assert assert_error(r, 404, "not_found") == "找不到 route nope"
    r = await client.get("/api/groups/sk-test-0000abcd")
    msg = assert_error(r, 404, "not_found")
    assert "sk-test-0000" not in msg
    r = await client.post("/api/estimate", json={"model": "sk-test-0000abcd",
                                                 "messages": [{"role": "user", "content": "x"}]})
    msg = assert_error(r, 404, "not_found")
    assert "sk-test-0000" not in msg


async def test_member_not_in_target_redacts_ids(client):
    r = await client.post("/api/routes", json={"name": "r1", "target": {"tags": {"all": ["zz"]}}})
    assert r.status_code == 201, r.text
    r = await client.put("/api/routes/r1/order",
                         json={"slots": [[{"deployment": "sk-test-0000abcd"}]]})
    msg = assert_error(r, 422, "member_not_in_target")
    assert "sk-test-0000" not in msg


async def test_extra_forbidden_key_name_redacted(client):
    r = await client.post("/api/deployments",
                          json={**api_member("a"), "sk-test-0000abcd": 1})
    msg = assert_error(r, 422, "validation_error")
    assert "sk-test-0000" not in msg

"""`GET /api/adapters`(spec 9.2.2、7.6.4、7.6.7;design D13;決策 25)(CHG-20261030-02 T14)。"""
from __future__ import annotations

import pytest

from tests.contract.fakemods import write_module

from .helpers import api_member, local_member

pytestmark = pytest.mark.anyio

FIELDS = {"module", "builtin", "providers", "kinds", "access", "cardinality",
          "fallback_for_kinds", "existing_members", "available", "unavailable_reason",
          "models", "models_error"}


async def test_builtin_modules_listed_with_models(client):
    await client.post("/api/deployments", json=api_member("a"))
    await client.post("/api/deployments", json=api_member("b"))
    await client.post("/api/deployments", json=api_member("c", provider="anthropic",
                                                          model="claude-sonnet-4-5"))
    await client.post("/api/deployments", json=local_member("q"))
    await client.post("/api/deployments", json=api_member("l", provider="litellm",
                                                          model="openai/gpt-4o-mini"))
    r = await client.get("/api/adapters")
    assert r.status_code == 200, r.text
    mods = {m["module"]: m for m in r.json()["modules"]}
    assert {"anthropic_api", "openai_api", "litellm"} <= set(mods)
    for m in mods.values():
        assert set(m) == FIELDS
        assert m["available"] is True and m["unavailable_reason"] is None
        assert m["cardinality"] == "multiple"
        for model in m["models"]:
            assert {"id", "label", "capabilities"} <= set(model)
            assert set(model["capabilities"]["efforts"]) <= {"none", "low", "medium", "high",
                                                              "max"}
    oa = mods["openai_api"]
    assert oa["providers"] == ["openai"] and oa["kinds"] == ["api"] and oa["access"] == ["api"]
    assert oa["existing_members"] == 2 and oa["builtin"] is True and oa["models"]
    assert mods["anthropic_api"]["existing_members"] == 1
    # litellm:自己宣告的 litellm provider + 後備的 hosted_vllm
    assert mods["litellm"]["existing_members"] == 2
    assert mods["litellm"]["fallback_for_kinds"] == ["api", "local"]


async def test_litellm_models_merge_local_endpoint(client, fake_api):
    """決策 25:litellm 模組以 SPILLWAY_LOCAL_API_BASE 查 local 端點,與 models.yaml 合併。"""
    lit = next(m for m in (await client.get("/api/adapters")).json()["modules"]
               if m["module"] == "litellm")
    assert lit["models_error"] is None
    ids = [m["id"] for m in lit["models"]]
    from tests.fakes.api_upstream.server import DEFAULT_MODELS
    assert ids[:3] == ["deepseek-chat", "deepseek-reasoner", "gemini-2.5-flash"]  # models.yaml 在前
    assert set(DEFAULT_MODELS) <= set(ids)
    assert any(r.path == "/v1/models" for r in fake_api.requests)


async def test_litellm_models_error_when_endpoint_down(client, monkeypatch):
    monkeypatch.setenv("SPILLWAY_LOCAL_API_BASE", "http://127.0.0.1:9/v1")
    lit = next(m for m in (await client.get("/api/adapters")).json()["modules"]
               if m["module"] == "litellm")
    assert lit["models_error"]                      # 附原因,仍回 models.yaml
    assert lit["available"] is True


async def test_extra_single_and_broken_modules(tmp_path, make_app, make_client):
    root = tmp_path / "mods"
    write_module(root, "solo", providers=("solo-cli",), kinds=("cli",),
                 access=("subscription-cli",), cardinality="single")
    write_module(root, "broken", pre_import="import not_a_real_pkg_xyz\n",
                 providers=("broken-p",))
    async with make_client(make_app(extra_paths=[root])) as c:
        await c.post("/api/deployments", json={"id": "s1", "model_name": "s1", "kind": "cli",
                                               "provider": "solo-cli", "model": "fake-small"})
        await c.post("/api/deployments", json=api_member("b1", provider="broken-p"))
        mods = {m["module"]: m for m in (await c.get("/api/adapters")).json()["modules"]}
    solo = mods["solo"]
    assert solo["cardinality"] == "single" and solo["existing_members"] == 1
    assert solo["builtin"] is False and solo["access"] == ["subscription-cli"]
    assert [m["id"] for m in solo["models"]] == ["fake-small", "fake-large"]
    broken = mods["broken"]
    assert broken["available"] is False and "broken" in broken["unavailable_reason"]
    assert broken["providers"] == ["broken-p"] and broken["existing_members"] == 1
    assert broken["models"] == [] and broken["cardinality"] is None

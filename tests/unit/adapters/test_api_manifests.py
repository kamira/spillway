"""T6 L1:三個內建 API 模組的 MANIFEST、models.yaml,與 credential_ref 為空的 api 成員。

spec 6.3、7.6.2、7.6.4、7.6.9;決策 13。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from spillway.adapters.base import (
    API_DEFAULT_SIGNALS,
    EFFORTS,
    AdapterRegistry,
    load_models_yaml,
    validate_manifest,
)

from .cases import API_CASES, MODULE_NAMES, Resolver, adapter, context, dep, module, request

pytestmark = pytest.mark.anyio

EXPECTED = {
    "anthropic_api": dict(providers=("anthropic",), kinds=("api",), access=("api",), fallback=()),
    "openai_api": dict(providers=("openai",), kinds=("api",), access=("api",), fallback=()),
    "litellm": dict(providers=("litellm",), kinds=("api", "local"), access=("api", "local"),
                    fallback=("api", "local")),
}


def _models(name: str):
    return load_models_yaml(Path(module(name).__file__).parent / "models.yaml")


# ---- L1:effort_map ----------------------------------------------------------------------

@pytest.mark.parametrize("name", MODULE_NAMES)
def test_effort_map_none_is_null_and_max_is_max(name):
    m = validate_manifest(module(name).MANIFEST)
    assert m.effort_map["none"] is None
    assert m.effort_map["max"] == "max"


@pytest.mark.parametrize("name", MODULE_NAMES)
def test_effort_map_matches_spec_6_3(name):
    m = validate_manifest(module(name).MANIFEST)
    assert dict(m.effort_map) == {"none": None, "low": "low", "medium": "medium",
                                  "high": "high", "max": "max"}
    assert tuple(m.effort_map) == EFFORTS


# ---- MANIFEST 其他欄位(spec 7.6.9;T6 interfaces)------------------------------------------

@pytest.mark.parametrize("name", MODULE_NAMES)
def test_manifest_matches_7_6_9(name):
    m = validate_manifest(module(name).MANIFEST)
    exp = EXPECTED[name]
    assert m.providers == exp["providers"]
    assert m.kinds == exp["kinds"]
    assert m.access == exp["access"]
    assert m.fallback_for_kinds == exp["fallback"]
    assert m.cardinality == "multiple"
    assert m.contract_version == 1


@pytest.mark.parametrize("name", MODULE_NAMES)
def test_requires_lists_no_api_key_env(name):
    m = validate_manifest(module(name).MANIFEST)
    assert m.requires == ()
    assert not any("KEY" in r.upper() or "TOKEN" in r.upper() for r in m.requires)


@pytest.mark.parametrize("name", MODULE_NAMES)
def test_default_signals_are_spec_api_row(name):
    m = validate_manifest(module(name).MANIFEST)
    assert dict(m.default_signals) == API_DEFAULT_SIGNALS


@pytest.mark.parametrize("name", MODULE_NAMES)
def test_default_timeout_is_120(name):
    assert validate_manifest(module(name).MANIFEST).default_params["timeout"] == 120


def test_registry_routes_providers_to_the_three_modules():
    r = AdapterRegistry(context())
    r.discover()
    names = {m.name for m in r.modules() if m.available}
    assert set(MODULE_NAMES) <= names
    from spillway.adapters.base import Deployment

    def mod_for(provider, kind):
        return r.module_for(Deployment(id="x", model_name="x", kind=kind, provider=provider, model="m")).name

    assert mod_for("anthropic", "api") == "anthropic_api"
    assert mod_for("openai", "api") == "openai_api"
    assert mod_for("deepseek", "api") == "litellm"        # 未被宣告的 api provider → 後備
    assert mod_for("hosted_vllm", "local") == "litellm"   # 未被宣告的 local provider → 後備
    assert mod_for("litellm", "local") == "litellm"
    assert r.cardinality_of("anthropic") == r.cardinality_of("openai") == "multiple"
    assert r.cardinality_of("ollama", "local") == "multiple"


# ---- L1:models.yaml ---------------------------------------------------------------------

@pytest.mark.parametrize("name", MODULE_NAMES)
def test_models_yaml_entries_have_id_label_capabilities(name):
    for m in _models(name):
        assert m.id and m.label
        assert {"ctx", "tools", "vision", "efforts"} <= set(m.capabilities)
        assert set(m.capabilities["efforts"]) <= set(EFFORTS)


@pytest.mark.parametrize("name", ["anthropic_api", "openai_api"])
def test_dedicated_modules_list_several_models(name):
    assert len(_models(name)) >= 3


@pytest.mark.parametrize("name", MODULE_NAMES)
def test_models_yaml_ids_are_priced_by_litellm(name):
    import litellm

    provider = {"anthropic_api": "anthropic", "openai_api": "openai"}.get(name)
    for m in _models(name):
        p = m.provider or provider
        key = m.id if name != "litellm" else f"{p}/{m.id}"
        assert key in litellm.model_cost, f"LiteLLM 沒有 {key} 的定價"


# ---- L1:credential_ref 為空的 api 成員 ---------------------------------------------------

@pytest.mark.parametrize("case", API_CASES, ids=[c.id for c in API_CASES])
@pytest.mark.parametrize("ref", [None, "", "   "])
async def test_api_member_without_credential_ref_errors_without_request(case, ref, fake_api):
    resolver = Resolver()
    r = await adapter(case, resolver).call(dep(case, fake_api.base_url, credential_ref=ref),
                                           request(), None)
    assert r.status == "error" and r.ok is False
    assert r.error and "credential_ref" in r.error
    assert fake_api.requests == []
    assert resolver.calls == []

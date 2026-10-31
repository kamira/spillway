"""AdapterManifest 驗證與 models.yaml 解析(spec 7.6.2、7.6.4;T4 L1)。"""
from __future__ import annotations

import pytest

from spillway.adapters.base import (
    REQUIRED_MANIFEST_FIELDS,
    AdapterManifest,
    ManifestError,
    ModelInfo,
    ModelsYamlError,
    load_models_yaml,
    parse_models,
    validate_manifest,
)


def good() -> dict:
    return {
        "providers": ["acme"],
        "kinds": ["api"],
        "access": ["api"],
        "cardinality": "multiple",
        "effort_map": {"none": None, "low": "low", "medium": "medium", "high": "high", "max": "max"},
        "default_signals": {"quota": [{"http_status": 402}], "rate_limit": [{"http_status": 429}]},
        "default_capabilities": {"ctx": 8000, "tools": True, "vision": False, "efforts": ["low"]},
        "default_params": {"timeout": 120},
        "requires": [],
        "contract_version": 1,
    }


def test_valid_dict_returns_normalized_manifest():
    m = validate_manifest(good())
    assert isinstance(m, AdapterManifest)
    assert m.providers == ("acme",)
    assert m.kinds == ("api",)
    assert m.access == ("api",)
    assert m.fallback_for_kinds == ()
    assert m.effort_map["none"] is None


def test_manifest_instance_is_accepted():
    m = validate_manifest(AdapterManifest(**good()))
    assert m.providers == ("acme",)


def test_required_fields_match_spec_7_6_2():
    assert set(REQUIRED_MANIFEST_FIELDS) == {
        "providers", "kinds", "access", "cardinality", "effort_map", "default_signals",
        "default_capabilities", "default_params", "requires", "contract_version",
    }


@pytest.mark.parametrize("field", sorted(good()))
def test_missing_field_fails_with_field_name(field):
    d = good()
    del d[field]
    with pytest.raises(ManifestError) as ei:
        validate_manifest(d)
    assert ei.value.field == field
    assert field in str(ei.value)


def test_cardinality_must_be_single_or_multiple():
    d = good() | {"cardinality": "many"}
    with pytest.raises(ManifestError) as ei:
        validate_manifest(d)
    assert ei.value.field == "cardinality"


@pytest.mark.parametrize("v", [2, 0, "1", True])
def test_contract_version_other_than_1_fails(v):
    with pytest.raises(ManifestError) as ei:
        validate_manifest(good() | {"contract_version": v})
    assert ei.value.field == "contract_version"


def test_effort_map_missing_level_fails():
    d = good()
    del d["effort_map"]["max"]
    with pytest.raises(ManifestError) as ei:
        validate_manifest(d)
    assert ei.value.field == "effort_map.max"


def test_effort_map_extra_level_fails():
    d = good()
    d["effort_map"]["xhigh"] = "xhigh"
    with pytest.raises(ManifestError) as ei:
        validate_manifest(d)
    assert ei.value.field == "effort_map.xhigh"


def test_effort_map_value_must_be_str_or_none():
    d = good()
    d["effort_map"]["low"] = 3
    with pytest.raises(ManifestError) as ei:
        validate_manifest(d)
    assert ei.value.field == "effort_map.low"


@pytest.mark.parametrize(
    "field,value",
    [
        ("providers", []),
        ("providers", [""]),
        ("providers", "acme"),
        ("kinds", ["grpc"]),
        ("kinds", []),
        ("access", ["paid"]),
        ("requires", [1]),
        ("default_params", []),
        ("default_capabilities", {"efforts": ["ultra"]}),
        ("default_signals", {"quota": [{"bogus": 1}]}),
        ("fallback_for_kinds", ["local"]),   # 不在 kinds 內
    ],
)
def test_invalid_values_fail_with_field_name(field, value):
    with pytest.raises(ManifestError) as ei:
        validate_manifest(good() | {field: value})
    assert ei.value.field.split(".")[0].split("[")[0] == field


def test_access_string_is_normalized():
    assert validate_manifest(good() | {"access": "api"}).access == ("api",)


def test_fallback_for_kinds_optional_and_valid():
    m = validate_manifest(good() | {"kinds": ["api", "local"], "access": ["api", "local"],
                                    "fallback_for_kinds": ["api", "local"]})
    assert m.fallback_for_kinds == ("api", "local")


def test_unknown_field_fails():
    with pytest.raises(ManifestError) as ei:
        validate_manifest(good() | {"provider": "typo"})
    assert ei.value.field == "provider"


def test_non_mapping_fails():
    with pytest.raises(ManifestError):
        validate_manifest(["not", "a", "manifest"])


# ---- models.yaml -------------------------------------------------------------------

MODELS = """\
- id: m1
  label: Model One
  capabilities: {ctx: 1000, tools: true, vision: false, efforts: [low, high]}
- id: m2
  label: Model Two
  provider: acme
  note: hi
  capabilities: {ctx: 2000, tools: false, vision: true, efforts: []}
"""


def test_load_models_yaml(tmp_path):
    p = tmp_path / "models.yaml"
    p.write_text(MODELS, encoding="utf-8")
    ms = load_models_yaml(p)
    assert [m.id for m in ms] == ["m1", "m2"]
    assert isinstance(ms[0], ModelInfo)
    assert ms[0].capabilities["efforts"] == ("low", "high")
    assert ms[1].provider == "acme" and ms[1].note == "hi"
    assert ms[0].to_dict()["capabilities"]["efforts"] == ["low", "high"]


def test_models_yaml_sixth_effort_fails(tmp_path):
    p = tmp_path / "models.yaml"
    p.write_text(MODELS.replace("[low, high]", "[low, xhigh]"), encoding="utf-8")
    with pytest.raises(ModelsYamlError) as ei:
        load_models_yaml(p)
    assert ei.value.field == "[0].capabilities.efforts"
    assert "xhigh" in str(ei.value)


@pytest.mark.parametrize(
    "data,field",
    [
        ({"id": "x"}, ""),                       # 頂層不是清單
        (["x"], "[0]"),
        ([{"label": "x", "capabilities": {"ctx": 1, "tools": True, "vision": True, "efforts": []}}], "[0].id"),
        ([{"id": "x", "capabilities": {"ctx": 1, "tools": True, "vision": True, "efforts": []}}], "[0].label"),
        ([{"id": "x", "label": "x"}], "[0].capabilities"),
        ([{"id": "x", "label": "x", "capabilities": {"tools": True, "vision": True, "efforts": []}}],
         "[0].capabilities.ctx"),
        ([{"id": "x", "label": "x", "capabilities": {"ctx": 1, "tools": "yes", "vision": True, "efforts": []}}],
         "[0].capabilities.tools"),
        ([{"id": "x", "label": "x", "capabilities": {"ctx": 1, "tools": True, "efforts": []}}],
         "[0].capabilities.vision"),
        ([{"id": "x", "label": "x", "capabilities": {"ctx": 1, "tools": True, "vision": True}}],
         "[0].capabilities.efforts"),
    ],
)
def test_models_entry_field_errors(data, field):
    with pytest.raises(ModelsYamlError) as ei:
        parse_models(data)
    assert ei.value.field == field


def test_models_duplicate_id_fails():
    entry = {"id": "x", "label": "x", "capabilities": {"ctx": 1, "tools": True, "vision": True, "efforts": []}}
    with pytest.raises(ModelsYamlError) as ei:
        parse_models([entry, dict(entry)])
    assert ei.value.field == "[1].id"


def test_models_yaml_syntax_error(tmp_path):
    p = tmp_path / "models.yaml"
    p.write_text("- id: [unclosed\n", encoding="utf-8")
    with pytest.raises(ModelsYamlError):
        load_models_yaml(p)


def test_models_yaml_empty_file_is_empty_list(tmp_path):
    p = tmp_path / "models.yaml"
    p.write_text("", encoding="utf-8")
    assert load_models_yaml(p) == []


def test_models_yaml_missing_file(tmp_path):
    with pytest.raises(ModelsYamlError):
        load_models_yaml(tmp_path / "nope.yaml")

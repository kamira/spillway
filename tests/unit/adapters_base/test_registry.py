"""AdapterRegistry 自動發現(spec 7.1.1、7.6.6、7.6.7;決策 14、15;T4 L2)。

測試模組一律寫在 tmp_path(決策 15),以 `discover(extra_paths=[...])` 載入。
"""
from __future__ import annotations

import pytest

from spillway.adapters.base import (
    AdapterConflictError,
    AdapterContext,
    AdapterRegistry,
    AdapterUnavailableError,
    Deployment,
    ModelInfo,
    available_models,
)
from spillway.clock import FakeClock
from tests.contract.fakemods import write_module

pytestmark = pytest.mark.anyio


@pytest.fixture
def ctx():
    return AdapterContext(credentials=lambda ref: "sk-test-0000", clock=FakeClock())


def dep(provider, kind="api", id="d1"):
    return Deployment(id=id, model_name=id, kind=kind, provider=provider, model="m")


def registry(ctx, *paths):
    r = AdapterRegistry(ctx)
    r.discover(extra_paths=list(paths))
    return r


def test_builtin_discovery_runs(ctx):
    r = AdapterRegistry(ctx)
    manifests = r.discover()
    assert isinstance(manifests, list)
    names = {m.name for m in r.modules()}
    assert "base" not in names


def test_valid_module_registered(tmp_path, ctx):
    write_module(tmp_path, "acme_mod", providers=("acme",))
    r = AdapterRegistry(ctx)
    manifests = r.discover(extra_paths=[tmp_path])
    assert any(m.providers == ("acme",) for m in manifests)
    info = next(m for m in r.modules() if m.name == "acme_mod")
    assert info.available and info.reason is None and not info.builtin
    assert info.providers == ("acme",)
    assert [m.id for m in info.models] == ["fake-small", "fake-large"]
    a = r.get(dep("acme"))
    assert a.context is ctx
    assert r.get(dep("acme")) is a          # 同一實例
    assert r.unavailable() == {}


def test_unimportable_module_unavailable_provider_from_manifest_literal(tmp_path, ctx):
    write_module(tmp_path, "fallback_mod", providers=("generic",), kinds=("api", "local"),
                 access=("api", "local"), fallback_for_kinds=("api", "local"))
    write_module(tmp_path, "broken_mod", providers=("broken",),
                 pre_import="import spillway_no_such_package_xyz\n")
    write_module(tmp_path, "acme_mod", providers=("acme",))
    r = registry(ctx, tmp_path)
    un = r.unavailable()
    assert set(un) == {"broken"}                       # 由 MANIFEST 字面值推得,不是 broken-mod
    assert "spillway_no_such_package_xyz" in un["broken"]
    with pytest.raises(AdapterUnavailableError) as ei:
        r.get(dep("broken"))                            # 不落到 litellm 後備
    assert ei.value.provider == "broken"
    assert r.get(dep("acme")) is not None               # 其他模組照常註冊
    assert r.get(dep("deepseek")) is not None           # 後備照常
    info = next(m for m in r.modules() if m.name == "broken_mod")
    assert not info.available and info.manifest is None and info.providers == ("broken",)


def test_unimportable_dict_manifest_literal(tmp_path, ctx):
    write_module(tmp_path, "dict_mod", init_source=(
        "import spillway_no_such_package_xyz\n"
        "MANIFEST = {'providers': ['dicty'], 'kinds': ['api']}\n"
    ))
    assert set(registry(ctx, tmp_path).unavailable()) == {"dicty"}


def test_unimportable_non_literal_manifest_uses_folder_name(tmp_path, ctx):
    write_module(tmp_path, "my_mod", init_source=(
        "import spillway_no_such_package_xyz\n"
        "MANIFEST = build_manifest()\n"
    ))
    assert set(registry(ctx, tmp_path).unavailable()) == {"my-mod"}


def test_syntax_error_module_unavailable(tmp_path, ctx):
    write_module(tmp_path, "syn_mod", init_source="MANIFEST = AdapterManifest(providers=('syn',)\n")
    assert set(registry(ctx, tmp_path).unavailable()) == {"syn-mod"}


def test_invalid_manifest_unavailable(tmp_path, ctx):
    write_module(tmp_path, "v2_mod", providers=("vtwo",), contract_version=2)
    r = registry(ctx, tmp_path)
    assert "contract_version" in r.unavailable()["vtwo"]


def test_missing_create_unavailable(tmp_path, ctx):
    write_module(tmp_path, "nocreate", init_source=(
        "from spillway.adapters.base import AdapterManifest\n"
        "MANIFEST = AdapterManifest(providers=('nc',), kinds=('api',), access=('api',),"
        " cardinality='multiple', effort_map={'none': None, 'low': None, 'medium': None,"
        " 'high': None, 'max': None}, default_signals={}, default_capabilities={},"
        " default_params={}, requires=(), contract_version=1)\n"
    ))
    assert "create" in registry(ctx, tmp_path).unavailable()["nc"]


def test_missing_manifest_unavailable(tmp_path, ctx):
    write_module(tmp_path, "noman_mod", init_source="def create(context):\n    return None\n")
    assert "MANIFEST" in registry(ctx, tmp_path).unavailable()["noman-mod"]


def test_bad_models_yaml_unavailable(tmp_path, ctx):
    write_module(tmp_path, "badmodels", providers=("bm",),
                 models_yaml="- id: a\n  label: A\n  capabilities: {ctx: 1, tools: true, vision: false, efforts: [ultra]}\n")
    assert "models.yaml" in registry(ctx, tmp_path).unavailable()["bm"]


def test_missing_models_yaml_unavailable(tmp_path, ctx):
    write_module(tmp_path, "nomodels", providers=("nm",), models_yaml=None)
    assert "models.yaml" in registry(ctx, tmp_path).unavailable()["nm"]


def test_create_raising_unavailable(tmp_path, ctx):
    write_module(tmp_path, "boom_mod", providers=("boom",),
                 adapter_source="class FakeAdapter:\n    def __init__(self, c):\n        raise RuntimeError('nope')\n")
    r = registry(ctx, tmp_path)
    assert "nope" in r.unavailable()["boom"]
    assert not next(m for m in r.modules() if m.name == "boom_mod").available


def test_sync_call_is_rejected(tmp_path, ctx):
    write_module(tmp_path, "sync_mod", providers=("sync",), adapter_source=(
        "class FakeAdapter:\n"
        "    def __init__(self, c):\n        pass\n"
        "    def call(self, dep, req, effort):\n        return None\n"
        "    async def health(self, dep):\n        return True\n"))
    assert "async" in registry(ctx, tmp_path).unavailable()["sync"]


def test_conflict_lists_both_modules(tmp_path, ctx):
    write_module(tmp_path, "first_mod", providers=("dup",))
    write_module(tmp_path, "second_mod", providers=("dup", "other"))
    r = AdapterRegistry(ctx)
    with pytest.raises(AdapterConflictError) as ei:
        r.discover(extra_paths=[tmp_path])
    msg = str(ei.value)
    assert "first_mod" in msg and "second_mod" in msg and "dup" in msg
    assert ei.value.provider == "dup"
    assert set(ei.value.modules) == {"first_mod", "second_mod"}


def test_conflict_across_extra_paths(tmp_path, ctx):
    a, b = tmp_path / "a", tmp_path / "b"
    write_module(a, "one", providers=("same",))
    write_module(b, "two", providers=("same",))
    with pytest.raises(AdapterConflictError):
        AdapterRegistry(ctx).discover(extra_paths=[a, b])


def test_conflicting_fallback_kinds(tmp_path, ctx):
    write_module(tmp_path, "fb1", providers=("g1",), fallback_for_kinds=("api",))
    write_module(tmp_path, "fb2", providers=("g2",), fallback_for_kinds=("api",))
    with pytest.raises(AdapterConflictError) as ei:
        AdapterRegistry(ctx).discover(extra_paths=[tmp_path])
    assert "fb1" in str(ei.value) and "fb2" in str(ei.value)


def test_undeclared_api_and_local_go_to_fallback(tmp_path, ctx):
    write_module(tmp_path, "generic", providers=("generic",), kinds=("api", "local"),
                 access=("api", "local"), fallback_for_kinds=("api", "local"))
    write_module(tmp_path, "acme_mod", providers=("acme",))
    r = registry(ctx, tmp_path)
    fb = r.get(dep("generic"))
    assert r.get(dep("deepseek", "api")) is fb
    assert r.get(dep("hosted_vllm", "local")) is fb
    assert r.get(dep("acme")) is not fb
    assert r.module_for(dep("deepseek")).name == "generic"
    with pytest.raises(AdapterUnavailableError):
        r.get(dep("some-cli", "cli"))                   # cli 沒有後備


def test_no_fallback_module_means_unavailable(tmp_path, ctx):
    write_module(tmp_path, "acme_mod", providers=("acme",))
    with pytest.raises(AdapterUnavailableError):
        registry(ctx, tmp_path).get(dep("deepseek"))


def test_kind_not_supported_by_module(tmp_path, ctx):
    write_module(tmp_path, "acme_mod", providers=("acme",), kinds=("api",))
    with pytest.raises(AdapterUnavailableError):
        registry(ctx, tmp_path).get(dep("acme", "local"))


def test_cardinality_of(tmp_path, ctx):
    write_module(tmp_path, "solo_mod", providers=("solo",), cardinality="single")
    write_module(tmp_path, "generic", providers=("generic",), kinds=("api", "local"),
                 access=("api", "local"), fallback_for_kinds=("api", "local"))
    write_module(tmp_path, "broken_mod", providers=("broken",), pre_import="import spillway_no_such_package_xyz\n")
    r = registry(ctx, tmp_path)
    assert r.cardinality_of("solo") == "single"
    assert r.cardinality_of("generic") == "multiple"
    assert r.cardinality_of("deepseek") == "multiple"          # 後備模組
    assert r.cardinality_of("deepseek", kind="api") == "multiple"
    assert r.cardinality_of("deepseek", kind="cli") is None
    assert r.cardinality_of("broken") is None


def test_rediscover_resets_state(tmp_path, ctx):
    a, b = tmp_path / "a", tmp_path / "b"
    write_module(a, "acme_mod", providers=("acme",))
    write_module(b, "broken_mod", providers=("broken",), pre_import="import spillway_no_such_package_xyz\n")
    r = AdapterRegistry(ctx)
    r.discover(extra_paths=[b])
    assert "broken" in r.unavailable()
    r.discover(extra_paths=[a])
    assert r.unavailable() == {}
    assert r.get(dep("acme")) is not None
    with pytest.raises(AdapterUnavailableError):
        r.get(dep("broken"))


def test_same_folder_name_in_two_paths_loads_separately(tmp_path, ctx):
    a, b = tmp_path / "a", tmp_path / "b"
    write_module(a, "mod", providers=("pa",))
    write_module(b, "mod", providers=("pb",))
    r = registry(ctx, a, b)
    assert r.get(dep("pa")) is not r.get(dep("pb"))


def test_get_without_context_raises(tmp_path):
    write_module(tmp_path, "acme_mod", providers=("acme",))
    r = AdapterRegistry()
    r.discover(extra_paths=[tmp_path])
    with pytest.raises(RuntimeError):
        r.get(dep("acme"))


def test_unavailable_returns_copy(tmp_path, ctx):
    write_module(tmp_path, "broken_mod", providers=("broken",), pre_import="import spillway_no_such_package_xyz\n")
    r = registry(ctx, tmp_path)
    r.unavailable().clear()
    assert "broken" in r.unavailable()


def test_nonexistent_extra_path_is_ignored(tmp_path, ctx):
    r = AdapterRegistry(ctx)
    r.discover(extra_paths=[tmp_path / "missing"])
    assert r.unavailable() == {}


def test_folders_without_init_and_private_are_skipped(tmp_path, ctx):
    (tmp_path / "notamodule").mkdir()
    write_module(tmp_path, "_private", providers=("priv",))
    r = registry(ctx, tmp_path)
    assert {m.name for m in r.modules()} & {"notamodule", "_private"} == set()


# ---- 單一模組壞掉不拖垮其他模組(審查 M7;spec 7.6.7)------------------------------------

def _assert_others_fine(r):
    assert r.get(dep("acme")) is not None
    assert next(m for m in r.modules() if m.name == "acme_mod").available


def test_big5_models_yaml_only_that_module_unavailable(tmp_path, ctx):
    d = write_module(tmp_path, "big5_mod", providers=("big5",))
    text = "- id: a\n  label: 測試模型\n  capabilities: {ctx: 1, tools: true, vision: false, efforts: []}\n"
    data = text.encode("big5")
    with pytest.raises(UnicodeDecodeError):
        data.decode("utf-8")
    (d / "models.yaml").write_bytes(data)
    write_module(tmp_path, "acme_mod", providers=("acme",))
    r = registry(ctx, tmp_path)
    assert set(r.unavailable()) == {"big5"}
    assert "models.yaml" in r.unavailable()["big5"] and "UTF-8" in r.unavailable()["big5"]
    _assert_others_fine(r)


def test_big5_models_yaml_raises_models_yaml_error(tmp_path):
    from spillway.adapters.base import ModelsYamlError, load_models_yaml
    p = tmp_path / "models.yaml"
    p.write_bytes("- id: 測試\n".encode("big5"))
    with pytest.raises(ModelsYamlError):
        load_models_yaml(p)


def test_sys_exit_at_import_only_that_module_unavailable(tmp_path, ctx):
    write_module(tmp_path, "exit_mod", providers=("exiter",), pre_import="import sys\nsys.exit(3)\n")
    write_module(tmp_path, "acme_mod", providers=("acme",))
    r = registry(ctx, tmp_path)
    assert set(r.unavailable()) == {"exiter"}
    assert "SystemExit" in r.unavailable()["exiter"]
    _assert_others_fine(r)


def test_sys_exit_in_create_only_that_module_unavailable(tmp_path, ctx):
    write_module(tmp_path, "exit_create", providers=("exitc",),
                 adapter_source="import sys\nclass FakeAdapter:\n    def __init__(self, c):\n        sys.exit(3)\n")
    write_module(tmp_path, "acme_mod", providers=("acme",))
    r = registry(ctx, tmp_path)
    assert "SystemExit" in r.unavailable()["exitc"]
    _assert_others_fine(r)


def test_keyboard_interrupt_at_import_still_propagates(tmp_path, ctx):
    write_module(tmp_path, "kbi_mod", providers=("kbi",), pre_import="raise KeyboardInterrupt\n")
    with pytest.raises(KeyboardInterrupt):
        registry(ctx, tmp_path)


def test_unexpected_error_after_import_only_that_module_unavailable(tmp_path, ctx):
    write_module(tmp_path, "weird_mod", init_source=(
        "def create(context):\n    return None\n"
        "def __getattr__(name):\n    raise RuntimeError('weird lookup')\n"))
    write_module(tmp_path, "acme_mod", providers=("acme",))
    r = registry(ctx, tmp_path)
    assert "weird lookup" in r.unavailable()["weird-mod"]
    _assert_others_fine(r)


def test_colliding_sanitized_names_get_distinct_import_names(tmp_path, ctx):
    """S-T4-4:`a-b` 與 `a_b` 經 \\W→_ 後同名,不可互相 purge。"""
    import sys
    write_module(tmp_path, "a-b", providers=("pdash",))
    write_module(tmp_path, "a_b", providers=("punder",))
    r = registry(ctx, tmp_path)
    infos = {m.name: m for m in r.modules()}
    assert infos["a-b"].available and infos["a_b"].available
    assert infos["a-b"].import_name != infos["a_b"].import_name
    for info in infos.values():
        assert sys.modules[info.import_name] is info.module
        assert sys.modules[info.import_name + ".adapter"].__file__.startswith(str(info.path))
    assert r.get(dep("pdash")) is not r.get(dep("punder"))


# ---- 可用模型清單(spec 7.6.4)--------------------------------------------------------

LIST_OK = """
async def list_models(context):
    return [{"id": "fake-small", "label": "dyn", "capabilities": {"ctx": 1, "tools": False, "vision": False, "efforts": []}},
            {"id": "dyn-only"}]
"""
LIST_FAIL = """
async def list_models(context):
    raise ConnectionError("endpoint down")
"""


async def test_available_models_static_only(tmp_path, ctx):
    write_module(tmp_path, "acme_mod", providers=("acme",))
    info = registry(ctx, tmp_path).modules()[-1]
    listing = await available_models(info, ctx)
    assert [m.id for m in listing.models] == ["fake-small", "fake-large"]
    assert listing.error is None


async def test_available_models_merges_dynamic(tmp_path, ctx):
    from tests.contract.fakemods import manifest_source
    init = ("from spillway.adapters.base import AdapterManifest\nfrom .adapter import FakeAdapter\n"
            + manifest_source(providers=("dyn",)) + "\ndef create(context):\n    return FakeAdapter(context)\n"
            + LIST_OK)
    write_module(tmp_path, "dyn_mod", init_source=init)
    info = next(m for m in registry(ctx, tmp_path).modules() if m.name == "dyn_mod")
    listing = await available_models(info, ctx)
    ids = [m.id for m in listing.models]
    assert ids == ["fake-small", "fake-large", "dyn-only"]
    small = listing.models[0]
    assert small.label == "Fake Small" and small.capabilities["ctx"] == 8000  # 同 id 以 models.yaml 為準
    dyn = listing.models[2]
    assert isinstance(dyn, ModelInfo) and dyn.label == "dyn-only"
    assert dyn.capabilities["efforts"] == ("low", "medium")  # 取 default_capabilities
    assert listing.error is None


async def test_available_models_failure_returns_static_with_reason(tmp_path, ctx):
    from tests.contract.fakemods import manifest_source
    init = ("from spillway.adapters.base import AdapterManifest\nfrom .adapter import FakeAdapter\n"
            + manifest_source(providers=("dynf",)) + "\ndef create(context):\n    return FakeAdapter(context)\n"
            + LIST_FAIL)
    write_module(tmp_path, "dynf_mod", init_source=init)
    info = next(m for m in registry(ctx, tmp_path).modules() if m.name == "dynf_mod")
    listing = await available_models(info, ctx)
    assert [m.id for m in listing.models] == ["fake-small", "fake-large"]
    assert "endpoint down" in listing.error


LIST_NONES = """
async def list_models(context):
    return [{"id": "none-caps", "label": None, "capabilities": None}, {"id": "plain"}]
"""


async def test_available_models_none_values_use_defaults(tmp_path, ctx):
    """S-T4-3:執行期項目中值為 None 的鍵改用預設,不讓整份清單作廢。"""
    from tests.contract.fakemods import manifest_source
    init = ("from spillway.adapters.base import AdapterManifest\nfrom .adapter import FakeAdapter\n"
            + manifest_source(providers=("dynn",)) + "\ndef create(context):\n    return FakeAdapter(context)\n"
            + LIST_NONES)
    write_module(tmp_path, "dynn_mod", init_source=init)
    info = next(m for m in registry(ctx, tmp_path).modules() if m.name == "dynn_mod")
    listing = await available_models(info, ctx)
    assert listing.error is None
    assert [m.id for m in listing.models] == ["fake-small", "fake-large", "none-caps", "plain"]
    none_caps = listing.models[2]
    assert none_caps.label == "none-caps"
    assert none_caps.capabilities["efforts"] == ("low", "medium")

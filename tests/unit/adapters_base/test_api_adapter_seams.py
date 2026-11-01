"""`LiteLLMApiAdapter` 的替換點(CHG-20261101-02 決策 11,T6 審議修正)。

`litellm_call`、`model_string`、`probe_http` 只查實際類別定義所在的那一個模組;該模組沒有同名屬性
或屬性不可呼叫時,改用 `adapters.base` 的實作。不打任何網路:替換點一律換成本檔的假函式。

注意:本檔刻意不把 `litellm_call`、`model_string`、`probe_http` 匯入到模組命名空間,
(b) 的測試就是靠「定義類別的模組沒有這些名稱」來驗證退回 `adapters.base`。
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest

from spillway.adapters import base
from spillway.adapters.base import LiteLLMApiAdapter, LiteLLMResult
from spillway.adapters.base import api_adapter as api_adapter_mod
from tests.unit.adapters.cases import CASES, MODULE_NAMES, adapter, context, dep, request

pytestmark = pytest.mark.anyio
SEAM_NAMES = ("litellm_call", "model_string", "probe_http")
API_CASE = {c.module: c for c in CASES if c.kind == "api"}     # 每個模組各一個 api 案例
LOCAL_CASE = next(c for c in CASES if c.kind == "local")
BASE_URL = "http://127.0.0.1:9"                                 # 不會被連線:呼叫都被替換


def _adapter_module(name: str):
    return importlib.import_module(f"spillway.adapters.{name}.adapter")


class _Recorder:
    def __init__(self, label: str) -> None:
        self.label, self.calls = label, []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return LiteLLMResult(ok=True, text=self.label, tokens_in=1, tokens_out=1, cost_usd=0.0)


# ---- (a) 在一個內建模組替換 litellm_call,不影響另外兩個 --------------------------------------

@pytest.mark.parametrize("patched", MODULE_NAMES)
async def test_patching_one_module_does_not_affect_the_others(patched, monkeypatch):
    recorders = {name: _Recorder(name) for name in MODULE_NAMES}
    monkeypatch.setattr(_adapter_module(patched), "litellm_call", recorders[patched])
    # 另外兩個模組不替換:它們的屬性仍是 base.litellm_call,應打到真實入口。為了不打網路,
    # 把那兩個模組的屬性換成同一個「base 代表」記錄器,確認 patched 的替換沒有外溢到它們。
    stand_in = _Recorder("base")
    for name in MODULE_NAMES:
        if name != patched:
            assert _adapter_module(name).litellm_call is base.litellm_call
            monkeypatch.setattr(_adapter_module(name), "litellm_call", stand_in)

    results = {}
    for name in MODULE_NAMES:
        case = API_CASE[name]
        results[name] = await adapter(case).call(dep(case, BASE_URL), request(), None)

    assert results[patched].text == patched
    assert len(recorders[patched].calls) == 1
    others = [n for n in MODULE_NAMES if n != patched]
    assert all(results[n].text == "base" for n in others)
    assert len(stand_in.calls) == len(others)
    for name in others:
        assert recorders[name].calls == []


async def test_unpatched_builtin_modules_resolve_to_base_objects():
    for name in MODULE_NAMES:
        a = adapter(API_CASE[name])
        for seam in SEAM_NAMES:
            assert a._seam(seam) is getattr(base, seam), (name, seam)


# ---- (b) 定義在沒有這些名稱的模組的子類別,退回 base ------------------------------------------

class _LocalSubclass(LiteLLMApiAdapter):
    pass


def test_subclass_in_module_without_names_falls_back_to_base():
    assert not any(hasattr(sys.modules[__name__], n) for n in SEAM_NAMES)
    a = _LocalSubclass(context(), importlib.import_module("spillway.adapters.openai_api").MANIFEST)
    for seam in SEAM_NAMES:
        assert a._seam(seam) is getattr(base, seam)


async def test_subclass_in_module_without_names_calls_base_entry(monkeypatch):
    fallback = _Recorder("base")
    monkeypatch.setitem(api_adapter_mod._SEAMS, "litellm_call", fallback)
    case = API_CASE["openai_api"]
    a = _LocalSubclass(context(), importlib.import_module("spillway.adapters.openai_api").MANIFEST)
    r = await a.call(dep(case, BASE_URL), request(), None)
    assert r.ok and r.text == "base"
    assert len(fallback.calls) == 1


# ---- (c) 同名但不可呼叫的模組屬性,退回 base -------------------------------------------------

def test_non_callable_module_global_falls_back_to_base(monkeypatch):
    fake_mod = types.ModuleType("spillway_test_fake_seam_mod")
    for seam in SEAM_NAMES:
        setattr(fake_mod, seam, "not callable")
    monkeypatch.setitem(sys.modules, fake_mod.__name__, fake_mod)
    cls = type("FakeSeamAdapter", (LiteLLMApiAdapter,), {"__module__": fake_mod.__name__})
    a = cls(context(), importlib.import_module("spillway.adapters.openai_api").MANIFEST)
    for seam in SEAM_NAMES:
        assert a._seam(seam) is getattr(base, seam)


@pytest.mark.parametrize("value", [None, 0, "", "x"])
def test_non_callable_values_on_builtin_module_fall_back(value, monkeypatch):
    monkeypatch.setattr(_adapter_module("litellm"), "litellm_call", value)
    a = adapter(API_CASE["litellm"])
    assert a._seam("litellm_call") is base.litellm_call


# ---- (d) model_string、probe_http 的模組替換生效 ---------------------------------------------

@pytest.mark.parametrize("name", MODULE_NAMES)
async def test_model_string_override_takes_effect(name, monkeypatch):
    mod = _adapter_module(name)
    rec = _Recorder("ok")
    monkeypatch.setattr(mod, "litellm_call", rec)
    monkeypatch.setattr(mod, "model_string", lambda d: f"overridden/{d.model}")
    case = API_CASE[name]
    r = await adapter(case).call(dep(case, BASE_URL), request(), None)
    assert r.ok
    assert rec.calls[0]["model"] == f"overridden/{case.model}"


async def test_probe_http_override_takes_effect(monkeypatch):
    seen = []

    async def fake_probe(url):
        seen.append(url)
        return False

    monkeypatch.setattr(_adapter_module("litellm"), "probe_http", fake_probe)
    d = dep(LOCAL_CASE, BASE_URL)
    assert await adapter(LOCAL_CASE).health(d) is False
    assert seen == [d.params["api_base"]]

    async def fake_probe_up(url):
        return True

    monkeypatch.setattr(_adapter_module("litellm"), "probe_http", fake_probe_up)
    assert await adapter(LOCAL_CASE).health(d) is True

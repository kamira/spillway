"""T6:litellm 模組特有行為——local 健康檢查(決策 10)、`list_models()`(spec 7.6.4)、
provider `litellm` 直通完整模型字串。
"""
from __future__ import annotations

import time

import pytest

from spillway.adapters.base import AdapterRegistry, Deployment, available_models

from .cases import CASES, adapter, closed_port, context, dep, module, request, spy_litellm_call

pytestmark = pytest.mark.anyio
LOCAL = next(c for c in CASES if c.id == "litellm-local")


def _litellm_info():
    r = AdapterRegistry(context())
    r.discover()
    return next(m for m in r.modules() if m.name == "litellm")


# ---- health(local)----------------------------------------------------------------------

async def test_local_health_true_when_endpoint_responds(fake_api):
    assert await adapter(LOCAL).health(dep(LOCAL, fake_api.base_url)) is True


async def test_local_health_false_when_endpoint_down():
    d = Deployment(id="l", model_name="l", kind="local", provider="hosted_vllm", model="m",
                   params={"api_base": f"http://127.0.0.1:{closed_port()}/v1"})
    start = time.monotonic()
    assert await adapter(LOCAL).health(d) is False
    assert time.monotonic() - start < 3


async def test_local_health_false_without_api_base():
    d = Deployment(id="l", model_name="l", kind="local", provider="hosted_vllm", model="m")
    assert await adapter(LOCAL).health(d) is False


# ---- list_models ----------------------------------------------------------------------------

async def test_list_models_queries_local_endpoint(fake_api):
    ids = [m["id"] for m in await module("litellm").list_models(context(),
                                                                api_base=fake_api.base_url + "/v1")]
    assert "local-model" in ids
    assert any(r.path == "/v1/models" for r in fake_api.requests)


async def test_list_models_accepts_base_without_v1(fake_api):
    ids = [m["id"] for m in await module("litellm").list_models(context(), api_base=fake_api.base_url)]
    assert "local-model" in ids


async def test_list_models_uses_env_base_and_merges_with_models_yaml(fake_api, monkeypatch):
    monkeypatch.setenv("SPILLWAY_LOCAL_API_BASE", fake_api.base_url + "/v1")
    info = _litellm_info()
    listing = await available_models(info, context())
    ids = [m.id for m in listing.models]
    assert listing.error is None
    assert [m.id for m in info.models] == ids[: len(info.models)]   # models.yaml 在前、能力為準
    assert "local-model" in ids


async def test_list_models_failure_returns_models_yaml_with_reason(monkeypatch):
    monkeypatch.setenv("SPILLWAY_LOCAL_API_BASE", f"http://127.0.0.1:{closed_port()}/v1")
    info = _litellm_info()
    listing = await available_models(info, context())
    assert [m.id for m in listing.models] == [m.id for m in info.models]
    assert listing.error


async def test_list_models_raises_on_http_error(fake_api):
    # 不存在的路徑 → 404 → 例外(由 available_models 轉成原因)
    with pytest.raises(Exception):
        await module("litellm").list_models(context(), api_base=fake_api.base_url + "/x/v1")


# ---- provider `litellm`:直通完整模型字串 -----------------------------------------------------

async def test_litellm_provider_passes_full_model_string(fake_api, monkeypatch):
    spy = spy_litellm_call(monkeypatch, LOCAL)
    d = Deployment(id="p", model_name="p", kind="local", provider="litellm",
                   model="hosted_vllm/local-model", params={"api_base": fake_api.base_url + "/v1"})
    r = await adapter(LOCAL).call(d, request(), None)
    assert r.ok and r.cost_usd == 0
    assert spy.calls[0]["model"] == "hosted_vllm/local-model"


async def test_litellm_provider_requires_prefixed_model(fake_api):
    d = Deployment(id="p", model_name="p", kind="local", provider="litellm",
                   model="local-model", params={"api_base": fake_api.base_url + "/v1"})
    r = await adapter(LOCAL).call(d, request(), None)
    assert r.status == "error" and "<provider>/<model>" in (r.error or "")
    assert fake_api.requests == []

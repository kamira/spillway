"""T6 L2:三個內建 API 模組以 T5 假端點呼叫(spec 5.2.1、5.2.4、5.3.1、6.3、7.2;決策 10、13)。

每個案例真的經 LiteLLM 打到 127.0.0.1 的假上游;`spy_litellm_call` 只記錄參數、不取代呼叫。
"""
from __future__ import annotations

import time

import pytest

from .cases import (
    CASES,
    KEY,
    REF,
    Resolver,
    adapter,
    api_base,
    dep,
    request,
    spy_litellm_call,
)

pytestmark = pytest.mark.anyio
IDS = [c.id for c in CASES]


# ---- 狀態對應 ----------------------------------------------------------------------------

@pytest.mark.parametrize("case", CASES, ids=IDS)
@pytest.mark.parametrize(
    ("mode", "status"),
    [("ok", "ok"), ("http_402", "quota_exhausted"), ("insufficient_quota", "quota_exhausted"),
     ("http_429", "rate_limited"), ("http_500", "error")],
)
async def test_status_for_upstream_output(case, mode, status, fake_api):
    fake_api.set_mode(case.model, mode)
    r = await adapter(case).call(dep(case, fake_api.base_url), request(), None)
    assert r.status == status
    assert r.ok is (status == "ok")
    assert (r.error is None) is (status == "ok")
    assert len(fake_api.requests) == 1


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_timeout_param_overrides_default(case, fake_api, monkeypatch):
    spy = spy_litellm_call(monkeypatch, case)
    fake_api.set_mode(case.model, "delay", seconds=2.0)
    start = time.monotonic()
    r = await adapter(case).call(dep(case, fake_api.base_url, timeout=0.3), request(), None)
    assert r.status == "error" and not r.ok
    assert time.monotonic() - start < 1.8
    assert spy.calls[0]["timeout"] == pytest.approx(0.3)


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_default_timeout_is_120(case, fake_api, monkeypatch):
    spy = spy_litellm_call(monkeypatch, case)
    await adapter(case).call(dep(case, fake_api.base_url), request(), None)
    assert spy.calls[0]["timeout"] == 120


# ---- 成功:tokens 與 cost --------------------------------------------------------------------

@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_success_tokens_and_cost(case, fake_api):
    r = await adapter(case).call(dep(case, fake_api.base_url), request(), None)
    assert r.ok and r.status == "ok" and r.error is None
    assert r.text == "fake upstream ok"
    assert (r.tokens_in, r.tokens_out) == (11, 7)
    if case.kind == "local":
        assert r.cost_usd == 0          # spec 5.2.4
    else:
        assert r.cost_usd is not None and r.cost_usd > 0   # spec 5.2.1


async def test_unpriced_api_model_cost_is_none(fake_api):
    case = next(c for c in CASES if c.id == "openai_api")
    fake_api.set_mode("no-such-priced-model", "ok")
    r = await adapter(case).call(dep(case, fake_api.base_url, model="no-such-priced-model"),
                                 request(), None)
    assert r.ok and r.cost_usd is None   # spec 5.2.1:無定價寫 NULL


# ---- 模型字串、api_base、憑證 ----------------------------------------------------------------

@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_model_string_and_api_base(case, fake_api, monkeypatch):
    spy = spy_litellm_call(monkeypatch, case)
    await adapter(case).call(dep(case, fake_api.base_url), request(), None)
    kw = spy.calls[0]
    assert kw["model"] == f"{case.provider}/{case.model}"
    assert kw["api_base"] == api_base(case, fake_api.base_url)
    rec = fake_api.requests[0]
    assert rec.model == case.model
    assert rec.path == ("/v1/messages" if case.provider == "anthropic" else "/v1/chat/completions")


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_model_starting_with_provider_prefix_is_still_prefixed(case, fake_api, monkeypatch):
    """M11:spec 7.2.1 沒有「已帶前綴就不補」的例外;模型 id 本身以 provider 名開頭時
    (vLLM 以 HF id 對外提供的模型)照補,上游收到完整的模型 id。"""
    spy = spy_litellm_call(monkeypatch, case)
    model_id = f"{case.provider}/{case.model}"
    fake_api.set_mode(model_id, "ok")
    r = await adapter(case).call(dep(case, fake_api.base_url, model=model_id), request(), None)
    assert r.ok, r.error
    assert spy.calls[0]["model"] == f"{case.provider}/{model_id}"
    assert fake_api.requests[0].model == model_id


async def test_openai_hf_style_model_id_reaches_upstream_intact(fake_api, monkeypatch):
    """M11 探測 t6b 的情境:provider=openai、model=openai/gpt-oss-20b。"""
    case = next(c for c in CASES if c.id == "openai_api")
    spy = spy_litellm_call(monkeypatch, case)
    fake_api.set_mode("openai/gpt-oss-20b", "ok")
    r = await adapter(case).call(dep(case, fake_api.base_url, model="openai/gpt-oss-20b"),
                                 request(), None)
    assert r.ok, r.error
    assert spy.calls[0]["model"] == "openai/openai/gpt-oss-20b"
    assert fake_api.requests[0].model == "openai/gpt-oss-20b"


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_credential_resolved_via_context_and_passed(case, fake_api, monkeypatch):
    spy = spy_litellm_call(monkeypatch, case)
    resolver = Resolver()
    r = await adapter(case, resolver).call(dep(case, fake_api.base_url, credential_ref=REF),
                                           request(), None)
    assert r.ok
    assert resolver.calls == [REF]
    assert spy.calls[0]["api_key"] == KEY
    assert fake_api.requests[0].has_auth


async def test_local_without_credential_ref_passes_none(fake_api, monkeypatch):
    case = next(c for c in CASES if c.kind == "local")
    spy = spy_litellm_call(monkeypatch, case)
    resolver = Resolver()
    r = await adapter(case, resolver).call(dep(case, fake_api.base_url, credential_ref=None),
                                           request(), None)
    assert r.ok
    assert resolver.calls == []
    assert "api_key" in spy.calls[0] and spy.calls[0]["api_key"] is None


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_credential_resolution_failure_is_error_without_request(case, fake_api):
    r = await adapter(case, Resolver(fail=True)).call(
        dep(case, fake_api.base_url, credential_ref=REF), request(), None)
    assert r.status == "error" and not r.ok
    assert fake_api.requests == []


# ---- params.signals 覆寫 ---------------------------------------------------------------------

@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_params_signals_override(case, fake_api):
    fake_api.set_mode(case.model, "http_500")
    d = dep(case, fake_api.base_url, signals={"rate_limit": [{"http_status": 500}]})
    assert (await adapter(case).call(d, request(), None)).status == "rate_limited"

    fake_api.set_mode(case.model, "http_402")
    d = dep(case, fake_api.base_url, signals={"quota": []})
    assert (await adapter(case).call(d, request(), None)).status == "error"


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_invalid_params_signals_is_error_without_request(case, fake_api):
    d = dep(case, fake_api.base_url, signals={"quota": "not-a-list"})
    r = await adapter(case).call(d, request(), None)
    assert r.status == "error" and "signals" in (r.error or "")
    assert fake_api.requests == []


# ---- 回顯模式:CallResult.error 不含金鑰 ------------------------------------------------------

@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_echoed_key_not_in_error(case, fake_api):
    # 憑證值由 credentials.resolve() strip 過(T3 M5);未 strip 的值連 HTTP header 都送不出去
    fake_api.set_mode(case.model, "echo_auth")
    r = await adapter(case, Resolver(value=KEY)).call(
        dep(case, fake_api.base_url, credential_ref=REF), request(), None)
    assert r.status == "error"
    assert r.error and "***" in r.error
    assert KEY not in r.error


# ---- effort ---------------------------------------------------------------------------------

@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_effort_high_sends_reasoning_effort_high(case, fake_api, monkeypatch):
    spy = spy_litellm_call(monkeypatch, case)
    model = case.effort_model or case.model
    r = await adapter(case).call(dep(case, fake_api.base_url, model=model), request(), "high")
    assert r.ok, r.error
    assert spy.calls[0]["reasoning_effort"] == "high"
    body = fake_api.requests[0].body or {}
    assert case.body_effort_key in body          # LiteLLM 依協定轉成上游參數
    if case.body_effort_key == "reasoning_effort":
        assert body["reasoning_effort"] == "high"


@pytest.mark.parametrize("case", CASES, ids=IDS)
@pytest.mark.parametrize("effort", ["none", None])
async def test_effort_none_not_sent(case, effort, fake_api, monkeypatch):
    spy = spy_litellm_call(monkeypatch, case)
    model = case.effort_model or case.model
    r = await adapter(case).call(dep(case, fake_api.base_url, model=model), request(), effort)
    assert r.ok
    assert spy.calls[0]["reasoning_effort"] is None
    body = fake_api.requests[0].body or {}
    assert "reasoning_effort" not in body and "thinking" not in body


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_unknown_effort_is_error_without_request(case, fake_api):
    r = await adapter(case).call(dep(case, fake_api.base_url), request(), "xhigh")
    assert r.status == "error"
    assert fake_api.requests == []


# ---- 請求內容轉交 ----------------------------------------------------------------------------

@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_request_fields_forwarded(case, fake_api, monkeypatch):
    spy = spy_litellm_call(monkeypatch, case)
    req = request(max_tokens=33, extra={"temperature": 0.2})
    await adapter(case).call(dep(case, fake_api.base_url), req, None)
    kw = spy.calls[0]
    assert kw["messages"] == req.messages
    assert kw["max_tokens"] == 33
    assert kw["extra"] == {"temperature": 0.2}


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_unsupported_kind_is_error_without_request(case, fake_api):
    from dataclasses import replace

    d = replace(dep(case, fake_api.base_url), kind="cli")
    r = await adapter(case).call(d, request(), None)
    assert r.status == "error"
    assert fake_api.requests == []


# ---- health ---------------------------------------------------------------------------------

@pytest.mark.parametrize("case", [c for c in CASES if c.kind == "api"], ids=lambda c: c.id)
async def test_api_member_health_is_true_without_request(case, fake_api):
    from dataclasses import replace

    d = replace(dep(case, fake_api.base_url), params={"api_base": "http://127.0.0.1:9/"})
    assert await adapter(case).health(d) is True
    assert fake_api.requests == []

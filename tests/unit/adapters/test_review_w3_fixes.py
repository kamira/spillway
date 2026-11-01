"""T6 審查回修(CHG-20261030-02 波次 3 審議 M11、S-T6-1;使用者決定 U3、U4)。

- U3:anthropic_api 的 `default_signals` 另含 400+`credit balance`(quota)與 529(rate_limit)。
- U4:上游只回工具呼叫時,`CallResult.tool_calls` 有值、`text` 為空字串、`ok=True`。
- S-T6-1 (a):三個模組的 `LiteLLMAdapter` 都是 `adapters.base.LiteLLMApiAdapter` 子類別且未覆寫
  call/health(CHG-20261101-02 決策 11 取代原本的逐字相同性測試)。
- M11:模型字串規則只在 `adapters.base.model_string`,adapter 不再自帶分支。
"""
from __future__ import annotations

import importlib

import litellm
import pytest

from spillway.adapters.base import LiteLLMResult

from .cases import CASES, KEY, REF, MODULE_NAMES, Resolver, adapter, dep, request

pytestmark = pytest.mark.anyio
IDS = [c.id for c in CASES]
ANTHROPIC = next(c for c in CASES if c.id == "anthropic_api")
OPENAI = next(c for c in CASES if c.id == "openai_api")


# ---- U3:anthropic 的額度用完與過載訊號 ------------------------------------------------------

def _fake_litellm_call(monkeypatch, case, raw):
    mod = importlib.import_module(f"spillway.adapters.{case.module}.adapter")

    async def fake(**_kwargs):
        return LiteLLMResult(ok=False, raw=raw)

    monkeypatch.setattr(mod, "litellm_call", fake)


@pytest.mark.parametrize(("raw", "status"), [
    ({"http_status": 400, "message": 'AnthropicException - {"type":"error","error":{"type":'
      '"invalid_request_error","message":"Your credit balance is too low to access the '
      'Anthropic API."}}'}, "quota_exhausted"),
    ({"http_status": 400, "message": "messages: field required"}, "error"),
    ({"http_status": 500, "message": "credit balance"}, "error"),     # 400 與訊息須同時成立
    ({"http_status": 529, "message": "Overloaded"}, "rate_limited"),
])
async def test_anthropic_credit_balance_and_overloaded(monkeypatch, raw, status):
    _fake_litellm_call(monkeypatch, ANTHROPIC, raw)
    r = await adapter(ANTHROPIC).call(dep(ANTHROPIC, "http://127.0.0.1:9"), request(), None)
    assert r.status == status


async def test_anthropic_529_from_fake_upstream_is_rate_limited(fake_api):
    """經假上游真的回 HTTP 529(echo_auth 模式可指定狀態碼),走完整 LiteLLM 例外路徑。"""
    fake_api.set_mode(ANTHROPIC.model, "echo_auth", status=529)
    r = await adapter(ANTHROPIC, Resolver(value=KEY)).call(
        dep(ANTHROPIC, fake_api.base_url, credential_ref=REF), request(), None)
    assert r.status == "rate_limited"
    assert KEY not in (r.error or "")


@pytest.mark.parametrize("case", [OPENAI, next(c for c in CASES if c.id == "litellm-api")],
                         ids=lambda c: c.id)
async def test_non_anthropic_modules_keep_spec_api_row(monkeypatch, case):
    _fake_litellm_call(monkeypatch, case, {"http_status": 400, "message": "credit balance"})
    assert (await adapter(case).call(dep(case, "http://127.0.0.1:9"), request(), None)).status == "error"
    _fake_litellm_call(monkeypatch, case, {"http_status": 529, "message": "Overloaded"})
    assert (await adapter(case).call(dep(case, "http://127.0.0.1:9"), request(), None)).status == "error"


# ---- U4:tool_calls 與 finish_reason ----------------------------------------------------------

TOOL_CALL = {"id": "call_1", "type": "function",
             "function": {"name": "get_weather", "arguments": '{"city": "Taipei"}'}}
TOOLS = [{"type": "function", "function": {"name": "get_weather", "parameters": {
    "type": "object", "properties": {"city": {"type": "string"}}}}}]


def _tool_response(model: str) -> litellm.ModelResponse:
    return litellm.ModelResponse(
        model=model,
        choices=[{"index": 0, "finish_reason": "tool_calls",
                  "message": {"role": "assistant", "content": None, "tool_calls": [TOOL_CALL]}}],
        usage={"prompt_tokens": 20, "completion_tokens": 9, "total_tokens": 29},
    )


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_tool_calls_only_response(case, monkeypatch):
    seen: list[dict] = []

    async def fake_acompletion(**kwargs):
        seen.append(kwargs)
        return _tool_response(case.model)

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    r = await adapter(case).call(dep(case, "http://127.0.0.1:9"), request(tools=TOOLS), None)
    assert r.ok is True and r.status == "ok" and r.error is None
    assert r.text == ""
    assert r.tool_calls == [TOOL_CALL]
    assert r.finish_reason == "tool_calls"
    assert (r.tokens_in, r.tokens_out) == (20, 9)
    assert seen[0]["tools"] == TOOLS


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_plain_text_response_has_no_tool_calls(case, fake_api):
    r = await adapter(case).call(dep(case, fake_api.base_url), request(), None)
    assert r.ok and r.text == "fake upstream ok"
    assert r.tool_calls is None
    assert r.finish_reason in ("stop", "end_turn")


# ---- S-T6-1 (a):三個模組共用 adapters.base.LiteLLMApiAdapter(CHG-20261101-02 決策 11)------

def test_builtin_adapters_subclass_shared_api_adapter_without_overrides():
    from spillway.adapters.base import LiteLLMApiAdapter

    for name in MODULE_NAMES:
        mod = importlib.import_module(f"spillway.adapters.{name}.adapter")
        cls = mod.LiteLLMAdapter
        assert issubclass(cls, LiteLLMApiAdapter), name
        assert cls.call is LiteLLMApiAdapter.call, f"{name} 不可覆寫 call(審議 S-T6-1)"
        assert cls.health is LiteLLMApiAdapter.health, f"{name} 不可覆寫 health(審議 S-T6-1)"


@pytest.mark.parametrize("name", MODULE_NAMES)
def test_adapters_use_base_model_string(name):
    from spillway.adapters import base

    mod = importlib.import_module(f"spillway.adapters.{name}.adapter")
    assert mod.model_string is base.model_string
    assert not hasattr(mod.LiteLLMAdapter, "model_string")

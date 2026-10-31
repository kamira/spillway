"""adapters.base 審查回修(CHG-20261030-02 波次 3:M11、M13;使用者決定 U3、U4)。不連網。

- `model_string(dep)`:spec 7.2.1 的唯一規則(adapter 與 estimate 共用)。
- `load_litellm()`:LiteLLM 載入與設定的唯一入口(含 `disable_hf_tokenizer_download`)。
- `litellm_call` 帶出 `tool_calls`、`finish_reason`。
- 例外鏈上的原始 HTTP 狀態碼(LiteLLM 把 Anthropic 529 改寫成 500)。
"""
from __future__ import annotations

from types import SimpleNamespace

import litellm
import pytest

from spillway.adapters.base import Deployment, classify, litellm_call, load_litellm, model_string
from spillway.adapters.base.litellm_call import exception_to_raw

pytestmark = pytest.mark.anyio

KEY = "sk-test-0000"
MSGS = [{"role": "user", "content": "hi"}]


def _dep(provider: str, model: str) -> Deployment:
    return Deployment(id="d", model_name="m", kind="api", provider=provider, model=model)


# ---- model_string -------------------------------------------------------------------------

@pytest.mark.parametrize(("provider", "model", "expected"), [
    ("openai", "gpt-4o-mini", "openai/gpt-4o-mini"),
    ("openai", "openai/gpt-oss-20b", "openai/openai/gpt-oss-20b"),       # M11:不去重
    ("anthropic", "anthropic/claude-x", "anthropic/anthropic/claude-x"),
    ("hosted_vllm", "meta-llama/Llama-3-8B", "hosted_vllm/meta-llama/Llama-3-8B"),
    ("litellm", "deepseek/deepseek-chat", "deepseek/deepseek-chat"),     # 直通原樣
    ("litellm", "ollama/llama3", "ollama/llama3"),
])
def test_model_string_rule(provider, model, expected):
    assert model_string(_dep(provider, model)) == expected


@pytest.mark.parametrize(("provider", "model"), [
    ("litellm", "llama3"), ("litellm", "/x"), ("litellm", "x/"), ("litellm", ""),
    ("openai", ""), ("openai", "   "),
])
def test_model_string_rejects_invalid(provider, model):
    with pytest.raises(ValueError):
        model_string(_dep(provider, model))


# ---- load_litellm -------------------------------------------------------------------------

def test_load_litellm_sets_guards(monkeypatch):
    monkeypatch.setattr(litellm, "disable_hf_tokenizer_download", False)
    monkeypatch.setattr(litellm, "set_verbose", True)
    assert load_litellm() is litellm
    assert litellm.disable_hf_tokenizer_download is True
    assert litellm.set_verbose is False and litellm.suppress_debug_info is True


async def test_litellm_call_sets_hf_download_guard(monkeypatch):
    async def fake_acompletion(**_kw):
        return SimpleNamespace(choices=[], usage=None)

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(litellm, "disable_hf_tokenizer_download", False)
    await litellm_call(model="openai/x", messages=MSGS, api_key=KEY)
    assert litellm.disable_hf_tokenizer_download is True


# ---- tool_calls / finish_reason -----------------------------------------------------------

async def test_tool_calls_and_finish_reason_extracted(monkeypatch):
    tc = {"id": "call_9", "type": "function", "function": {"name": "f", "arguments": '{"a": 1}'}}
    resp = litellm.ModelResponse(
        model="gpt-4o-mini",
        choices=[{"index": 0, "finish_reason": "tool_calls",
                  "message": {"role": "assistant", "content": None, "tool_calls": [tc]}}],
        usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    )

    async def fake_acompletion(**_kw):
        return resp

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    r = await litellm_call(model="openai/gpt-4o-mini", messages=MSGS, api_key=KEY)
    assert r.ok and r.text == ""
    assert r.tool_calls == [tc]
    assert all(type(t) is dict for t in r.tool_calls)
    assert r.finish_reason == "tool_calls"


async def test_dict_arguments_serialized_to_json_string(monkeypatch):
    resp = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(
            content=None, tool_calls=[{"id": "c", "function": {"name": "f", "arguments": {"a": 1}}}]))],
        usage=None,
    )

    async def fake_acompletion(**_kw):
        return resp

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    r = await litellm_call(model="openai/x", messages=MSGS, api_key=KEY)
    assert r.tool_calls == [{"id": "c", "type": "function",
                             "function": {"name": "f", "arguments": '{"a": 1}'}}]


async def test_no_tool_calls_is_none(monkeypatch):
    resp = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="x"))], usage=None)

    async def fake_acompletion(**_kw):
        return resp

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    r = await litellm_call(model="openai/x", messages=MSGS, api_key=KEY)
    assert r.ok and r.text == "x"
    assert r.tool_calls is None and r.finish_reason is None


# ---- 例外鏈上的原始狀態碼 --------------------------------------------------------------------

def test_status_prefers_original_upstream_status_in_chain():
    """LiteLLM 把 Anthropic 529 包成 status_code=500 的 InternalServerError;原值在例外鏈上。"""
    class Upstream(Exception):
        status_code = 529

    class Wrapped(Exception):
        status_code = 500

    try:
        try:
            raise Upstream("overloaded")
        except Upstream as inner:
            raise Wrapped("litellm.InternalServerError") from inner
    except Wrapped as exc:
        raw = exception_to_raw(exc)
    assert raw["http_status"] == 529
    assert classify(raw, {"rate_limit": [{"http_status": 529}]}) == "rate_limited"


def test_status_without_chain_uses_own_value():
    class E(Exception):
        status_code = 429

    assert exception_to_raw(E("x"))["http_status"] == 429

"""litellm_call():參數、verbose/callback、例外轉 raw dict(spec 7.2;決策 13;安全影響 e)。

不連網:以 monkeypatch 取代 `litellm.acompletion`。對假端點的真實呼叫在 T6 驗證。
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import litellm
import pytest

from spillway.adapters.base import API_DEFAULT_SIGNALS, LiteLLMResult, classify, litellm_call
from spillway.adapters.base.litellm_call import _scrub, exception_to_raw

pytestmark = pytest.mark.anyio

KEY = "sk-test-0000"
MSGS = [{"role": "user", "content": "hi"}]


def fake_response(text="hello", pin=11, pout=7):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=pin, completion_tokens=pout),
    )


@pytest.fixture
def captured(monkeypatch):
    calls: list[dict] = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        return fake_response()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0123)
    return calls


def raising(monkeypatch, exc):
    async def fake_acompletion(**kwargs):
        raise exc

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)


async def test_passes_explicit_params(captured):
    r = await litellm_call(model="openai/gpt-4o-mini", messages=MSGS, api_key=KEY,
                           api_base="http://127.0.0.1:9/v1")
    kw = captured[0]
    assert kw["model"] == "openai/gpt-4o-mini"
    assert kw["messages"] == MSGS
    assert kw["api_key"] == KEY
    assert kw["api_base"] == "http://127.0.0.1:9/v1"
    assert kw["timeout"] == 120
    assert kw["max_retries"] == 0
    assert kw["stream"] is False
    assert "reasoning_effort" not in kw
    assert isinstance(r, LiteLLMResult)
    assert r.ok and r.text == "hello" and r.tokens_in == 11 and r.tokens_out == 7
    assert r.cost_usd == pytest.approx(0.0123)
    assert r.raw is None


async def test_api_key_none_is_still_passed_explicitly(captured):
    await litellm_call(model="hosted_vllm/q", messages=MSGS, api_key=None)
    assert "api_key" in captured[0] and captured[0]["api_key"] is None
    assert "api_base" not in captured[0]


async def test_api_key_is_required_keyword():
    with pytest.raises(TypeError):
        await litellm_call(model="openai/x", messages=MSGS)  # type: ignore[call-arg]


async def test_timeout_override_and_optional_params(captured):
    await litellm_call(model="openai/x", messages=MSGS, api_key=KEY, timeout=5,
                       reasoning_effort="high", max_tokens=300,
                       tools=[{"type": "function", "function": {"name": "f"}}],
                       extra={"temperature": 0.2, "api_key": "sk-evil", "model": "other",
                              "stream": True, "timeout": 999})
    kw = captured[0]
    assert kw["timeout"] == 5
    assert kw["reasoning_effort"] == "high"
    assert kw["max_tokens"] == 300
    assert kw["tools"][0]["function"]["name"] == "f"
    assert kw["temperature"] == 0.2
    # extra 不可覆寫受保護的參數
    assert kw["api_key"] == KEY and kw["model"] == "openai/x"
    assert kw["stream"] is False and kw["timeout"] == 5


async def test_verbose_off_and_no_callbacks_registered(captured, monkeypatch):
    monkeypatch.setattr(litellm, "set_verbose", True)
    monkeypatch.setattr(litellm, "suppress_debug_info", False)
    before = {name: list(getattr(litellm, name)) for name in
              ("callbacks", "success_callback", "failure_callback", "input_callback")}
    await litellm_call(model="openai/x", messages=MSGS, api_key=KEY)
    assert litellm.set_verbose is False
    assert litellm.suppress_debug_info is True
    for name, value in before.items():
        assert list(getattr(litellm, name)) == value, name
    assert "callbacks" not in captured[0]
    assert "verbose" not in captured[0] or captured[0]["verbose"] is False


async def test_cost_none_when_unpriced(captured, monkeypatch):
    def boom(**kw):
        raise Exception("model not mapped")

    monkeypatch.setattr(litellm, "completion_cost", boom)
    r = await litellm_call(model="openai/x", messages=MSGS, api_key=KEY)
    assert r.ok and r.cost_usd is None


async def test_missing_usage_gives_none_tokens(monkeypatch):
    async def fake(**kw):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None))], usage=None)

    monkeypatch.setattr(litellm, "acompletion", fake)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: None)
    r = await litellm_call(model="openai/x", messages=MSGS, api_key=KEY)
    assert r.ok and r.text == "" and r.tokens_in is None and r.tokens_out is None


async def test_rate_limit_exception_to_raw(monkeypatch):
    raising(monkeypatch, litellm.RateLimitError("slow down", llm_provider="openai", model="x"))
    r = await litellm_call(model="openai/x", messages=MSGS, api_key=KEY)
    assert not r.ok
    assert r.raw["http_status"] == 429
    assert r.raw["exception"] == "RateLimitError"
    assert "slow down" in r.raw["message"]
    assert classify(r.raw, API_DEFAULT_SIGNALS) == "rate_limited"


async def test_payment_required_to_raw(monkeypatch):
    raising(monkeypatch, litellm.exceptions.PaymentRequiredError("pay up", model="x", llm_provider="openai"))
    r = await litellm_call(model="openai/x", messages=MSGS, api_key=KEY)
    assert r.raw["http_status"] == 402
    assert classify(r.raw, API_DEFAULT_SIGNALS) == "quota_exhausted"


async def test_insufficient_quota_code_from_message(monkeypatch):
    msg = 'OpenAIException - {"error": {"message": "You exceeded your quota", "type": "insufficient_quota", "code": "insufficient_quota"}}'
    raising(monkeypatch, litellm.RateLimitError(msg, llm_provider="openai", model="x"))
    r = await litellm_call(model="openai/x", messages=MSGS, api_key=KEY)
    assert r.raw["http_status"] == 429
    assert r.raw["error_code"] == "insufficient_quota"
    assert classify(r.raw, API_DEFAULT_SIGNALS) == "quota_exhausted"


async def test_error_code_from_body(monkeypatch):
    exc = RuntimeError("bad")
    exc.status_code = 400  # type: ignore[attr-defined]
    exc.body = {"error": {"code": "insufficient_quota"}}  # type: ignore[attr-defined]
    raising(monkeypatch, exc)
    r = await litellm_call(model="openai/x", messages=MSGS, api_key=KEY)
    assert r.raw["http_status"] == 400 and r.raw["error_code"] == "insufficient_quota"


async def test_timeout_exception(monkeypatch):
    raising(monkeypatch, litellm.Timeout("took too long", model="x", llm_provider="openai"))
    r = await litellm_call(model="openai/x", messages=MSGS, api_key=KEY, timeout=1)
    assert not r.ok
    assert r.raw["timeout"] is True
    assert classify(r.raw, API_DEFAULT_SIGNALS) == "error"


async def test_generic_exception_does_not_escape(monkeypatch):
    raising(monkeypatch, ConnectionError("refused"))
    r = await litellm_call(model="openai/x", messages=MSGS, api_key=KEY)
    assert not r.ok
    assert r.raw["http_status"] is None
    assert r.raw["exception"] == "ConnectionError"
    assert classify(r.raw, API_DEFAULT_SIGNALS) == "error"


async def test_api_key_scrubbed_from_raw_message(monkeypatch):
    raising(monkeypatch, litellm.exceptions.AuthenticationError(
        f"Incorrect API key provided: Bearer {KEY}", llm_provider="openai", model="x"))
    r = await litellm_call(model="openai/x", messages=MSGS, api_key=KEY)
    assert KEY not in r.raw["message"]
    assert KEY not in repr(r)
    assert r.raw["http_status"] == 401


async def test_raw_dict_keys(monkeypatch):
    raising(monkeypatch, ConnectionError("x"))
    r = await litellm_call(model="openai/x", messages=MSGS, api_key=KEY)
    assert set(r.raw) == {"http_status", "error_code", "message", "exception", "timeout"}


# ---- 金鑰遮罩的變體(審查 M6):上游回顯的金鑰形式不一定等於原值 -------------------------

LEAKY_KEYS = [
    pytest.param(" KEYzzz0002 ", id="surrounding-space"),
    pytest.param("KEYzzz0003\n", id="trailing-newline"),
    pytest.param('KEY"zz/0004', id="contains-quote"),
    pytest.param('  KEY"zz0005\t', id="quote-and-space"),
]


def _leak_forms(key: str) -> set[str]:
    core = key.strip()
    return {core, json.dumps(core)[1:-1], key, json.dumps(key)[1:-1]} - {""}


@pytest.mark.parametrize("model,base_suffix", [
    ("anthropic/claude-sonnet-4-5", ""),
    ("openai/gpt-4o-mini", "/v1"),
])
@pytest.mark.parametrize("key", LEAKY_KEYS)
async def test_echoed_key_variants_are_scrubbed(fake_api, model, base_suffix, key):
    fake_api.set_mode("*", "echo_auth")
    r = await litellm_call(model=model, messages=MSGS, api_key=key,
                           api_base=fake_api.base_url + base_suffix, timeout=10)
    assert not r.ok
    msg = r.raw["message"]
    if model.startswith("openai/") and key.endswith("\n"):
        # OpenAI SDK 拒絕送出含換行的 header,請求沒到上游(Connection error);仍不可含金鑰
        assert "invalid credential" not in msg
    else:
        assert "invalid credential" in msg and "***" in msg   # 確實有回顯,且已被遮
    for form in _leak_forms(key):
        assert form not in r.raw["message"], (form, r.raw["message"])
        assert form not in repr(r)


@pytest.mark.parametrize("key", [" sk-a ", "sk-b\n", 'sk-"c"', '\tsk-"d"\\e '])
def test_scrub_masks_all_variants(key):
    core = key.strip()
    text = f"raw={key} core={core} json={json.dumps(core)} jsonraw={json.dumps(key)}"
    out = _scrub(text, key)
    for form in _leak_forms(key):
        assert form not in out, (form, out)


def test_scrub_replaces_longest_variant_first():
    # 原值 " sk-x " 比 strip 後的 "sk-x" 長:先換長的,不會留下孤立的前後空白片段以外的殘片
    assert _scrub("[ sk-x ][sk-x]", " sk-x ") == "[***][***]"


def test_scrub_skips_empty_variants():
    assert _scrub("hello world", "") == "hello world"
    assert _scrub("hello world", None) == "hello world"
    assert _scrub("a  b", "  ") == "a***b"   # strip 後為空的變體略過,不會在每個位置插入遮罩


async def test_local_cost_map_guard_set_before_import(monkeypatch, captured):
    """延後 import litellm 前以 setdefault 設本地價格表(S-T4-1),已設定的值不覆寫。"""
    monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP", raising=False)
    await litellm_call(model="openai/x", messages=MSGS, api_key=KEY)
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    await litellm_call(model="openai/x", messages=MSGS, api_key=KEY)
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "true"


# ---- 嚴格整數解析(S-T4-2):怪異 status 不可讓 exception_to_raw 外拋 ------------------------

@pytest.mark.parametrize("value,expected", [
    ("429", 429), (" 500 ", 500), ("²", None), ("--5", None), ("", None), ("4x", None), ("١٢٣", None),
])
def test_exception_to_raw_status_string_never_raises(value, expected):
    class E(Exception):
        status_code = value

    assert exception_to_raw(E("boom"))["http_status"] == expected

"""T9 審查回修(CHG-20261030-02 波次 3 審議 M13、M14)。不連網。

- M14:estimate 查價與 token 計數用的模型字串,與 adapter 呼叫同一個 `adapters.base.model_string`。
- M13:llama 系模型名不可觸發 HuggingFace tokenizer 下載(`disable_hf_tokenizer_download`)。
"""
from __future__ import annotations

import litellm
import litellm.utils as lu
import pytest

import spillway.estimate as estimate_mod
from spillway.adapters import base
from spillway.domain import ChatRequest, Deployment
from spillway.estimate import Estimator

from tests.unit.adapters.cases import CASES

MSGS = [{"role": "user", "content": "hello world " * 10}]


def _req() -> ChatRequest:
    return ChatRequest(model="r", messages=MSGS)


# ---- M14:同一條模型字串規則 ------------------------------------------------------------------

def test_estimate_uses_the_same_model_string_function_as_adapters():
    assert estimate_mod.model_string is base.model_string
    for name in ("anthropic_api", "openai_api", "litellm"):
        mod = __import__(f"spillway.adapters.{name}.adapter", fromlist=["model_string"])
        assert mod.model_string is base.model_string


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_token_counter_receives_adapter_model_string(case, monkeypatch):
    seen: list[str] = []

    def spy(*, model, messages, **_kw):
        seen.append(model)
        return 42

    monkeypatch.setattr(litellm, "token_counter", spy)
    d = Deployment(id="d", model_name="m", kind=case.kind, provider=case.provider, model=case.model)
    assert Estimator(None).tokens_in(d, _req()) == 42
    assert seen == [base.model_string(d)]


def test_litellm_passthrough_member_is_priced():
    """provider=litellm、model=deepseek/deepseek-chat 有定價(探測 t9:原本得 None)。"""
    d = Deployment(id="d", model_name="m", kind="api", provider="litellm", model="deepseek/deepseek-chat")
    direct = Deployment(id="d2", model_name="m", kind="api", provider="deepseek", model="deepseek-chat")
    e = Estimator(None)
    cost = e.cost_usd(d, 100, 100)
    assert cost is not None and cost > 0
    assert cost == pytest.approx(e.cost_usd(direct, 100, 100))


def test_invalid_passthrough_model_is_unpriced_not_raising():
    d = Deployment(id="d", model_name="m", kind="api", provider="litellm", model="no-slash")
    assert Estimator(None).cost_usd(d, 100, 100) is None


# ---- M13:不從 HuggingFace 下載 tokenizer -----------------------------------------------------

@pytest.fixture
def hf_calls(monkeypatch):
    """攔截 litellm 實際的下載呼叫點 `litellm.utils.tokenizer_dispatch.from_pretrained`。"""
    calls: list[str] = []

    def fake_from_pretrained(name, *_a, **_kw):
        calls.append(name)
        raise RuntimeError("HuggingFace download blocked by test")

    monkeypatch.setattr(lu.tokenizer_dispatch, "from_pretrained", fake_from_pretrained)
    lu._load_huggingface_tokenizer.cache_clear()     # 先前快取的 tokenizer 不可掩蓋呼叫
    yield calls
    lu._load_huggingface_tokenizer.cache_clear()


LLAMA_DEPS = [
    Deployment(id="l3", model_name="m", kind="local", provider="hosted_vllm", model="meta-llama-3-8b"),
    Deployment(id="l2", model_name="m", kind="local", provider="ollama", model="llama-2-7b"),
]


def test_probe_hook_is_effective_without_guard(hf_calls, monkeypatch):
    """對照組:旗標關閉時,同樣的模型名確實會走到被攔截的下載點(證明攔截點正確)。"""
    monkeypatch.setattr(litellm, "disable_hf_tokenizer_download", False)
    for d in LLAMA_DEPS:
        try:
            litellm.token_counter(model=base.model_string(d), messages=MSGS)
        except Exception:  # noqa: BLE001
            pass
    assert hf_calls, "攔截點沒有被呼叫:litellm 的 tokenizer 下載路徑已變更,需更新本測試"


@pytest.mark.parametrize("d", LLAMA_DEPS, ids=lambda d: d.model)
def test_llama_models_do_not_download_tokenizer(d, hf_calls, monkeypatch):
    monkeypatch.setattr(litellm, "disable_hf_tokenizer_download", False)   # estimate 自己要設
    n = Estimator(None).tokens_in(d, _req())
    assert hf_calls == []
    assert isinstance(n, int) and n > 0
    assert litellm.disable_hf_tokenizer_download is True

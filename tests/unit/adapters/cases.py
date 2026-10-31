"""測試案例:每個內建 API 模組對假上游(tests/fakes/api_upstream)的呼叫設定。

`api_base` 依 LiteLLM 對接實測(tests/fakes/api_upstream/__init__.py):
openai/、hosted_vllm/、deepseek/ 要帶 `/v1`;anthropic/ 不帶。
"""
from __future__ import annotations

import importlib
import socket
from dataclasses import dataclass, field
from typing import Any

from spillway.adapters.base import AdapterContext, ChatRequest, Deployment
from spillway.clock import FakeClock

KEY = "sk-test-0000"
REF = "env:SPILLWAY_TEST_KEY"
MSGS = [{"role": "user", "content": "hi"}]


@dataclass(frozen=True)
class Case:
    id: str
    module: str          # adapters 子套件名
    kind: str
    provider: str
    model: str           # 成員 model(也是假上游看到的 model 名)
    v1: bool             # api_base 是否帶 /v1
    priced: bool         # LiteLLM 有定價(api)
    effort_model: str | None = None   # 送 reasoning_effort 用的模型(非推理模型會被 LiteLLM 拒絕)
    body_effort_key: str = "reasoning_effort"   # 假上游 body 中可觀察到 effort 的鍵


CASES = [
    Case("anthropic_api", "anthropic_api", "api", "anthropic", "claude-sonnet-4-5", v1=False,
         priced=True, body_effort_key="thinking"),
    Case("openai_api", "openai_api", "api", "openai", "gpt-4o-mini", v1=True, priced=True,
         effort_model="gpt-5.4-mini"),
    Case("litellm-api", "litellm", "api", "deepseek", "deepseek-chat", v1=True, priced=True,
         body_effort_key="thinking"),
    Case("litellm-local", "litellm", "local", "hosted_vllm", "local-model", v1=True, priced=False),
]
API_CASES = [c for c in CASES if c.kind == "api"]
MODULE_NAMES = ("anthropic_api", "openai_api", "litellm")


def module(name: str):
    return importlib.import_module(f"spillway.adapters.{name}")


class Resolver:
    """記錄被要求解析的 ref;回傳測試假金鑰。"""

    def __init__(self, value: str = KEY, fail: bool = False) -> None:
        self.value, self.fail, self.calls = value, fail, []

    def __call__(self, ref: str) -> str:
        self.calls.append(ref)
        if self.fail:
            raise LookupError("env 憑證的環境變數未設定或為空")
        return self.value


def context(resolver: Resolver | None = None) -> AdapterContext:
    return AdapterContext(credentials=resolver or Resolver(), clock=FakeClock())


def api_base(case: Case, base_url: str) -> str:
    return base_url + "/v1" if case.v1 else base_url


def dep(case: Case, base_url: str, *, model: str | None = None, credential_ref: Any = "default",
        **params: Any) -> Deployment:
    if credential_ref == "default":
        credential_ref = REF if case.kind == "api" else None
    p: dict[str, Any] = {"api_base": api_base(case, base_url)}
    p.update(params)
    return Deployment(id=f"d-{case.id}", model_name=f"m-{case.id}", kind=case.kind,
                      provider=case.provider, model=model or case.model, params=p,
                      credential_ref=credential_ref)


def adapter(case: Case, resolver: Resolver | None = None):
    return module(case.module).create(context(resolver))


def request(**kw: Any) -> ChatRequest:
    return ChatRequest(model="anything", messages=MSGS, **kw)


def closed_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@dataclass
class Spy:
    """包住 adapter 模組命名空間中的 `litellm_call`,記錄參數後照常呼叫(仍打假上游)。"""

    calls: list[dict[str, Any]] = field(default_factory=list)


def spy_litellm_call(monkeypatch, case: Case) -> Spy:
    mod = importlib.import_module(f"spillway.adapters.{case.module}.adapter")
    real = mod.litellm_call
    spy = Spy()

    async def wrapper(**kwargs):
        spy.calls.append(kwargs)
        return await real(**kwargs)

    monkeypatch.setattr(mod, "litellm_call", wrapper)
    return spy

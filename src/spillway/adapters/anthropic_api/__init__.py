"""anthropic_api:以 API key 經 LiteLLM 呼叫 Anthropic Messages API(spec 7.2、7.6.9)。"""
from __future__ import annotations

import copy

from spillway.adapters.base import API_DEFAULT_SIGNALS, AdapterContext, AdapterManifest

from .adapter import LiteLLMAdapter

MANIFEST = AdapterManifest(
    providers=("anthropic",),
    kinds=("api",),
    access=("api",),
    cardinality="multiple",
    effort_map={"none": None, "low": "low", "medium": "medium", "high": "high", "max": "max"},
    default_signals=copy.deepcopy(API_DEFAULT_SIGNALS),
    default_capabilities={"ctx": 200000, "tools": True, "vision": True,
                          "efforts": ["low", "medium", "high", "max"]},
    default_params={"timeout": 120},
    requires=(),
    contract_version=1,
)


def create(context: AdapterContext) -> LiteLLMAdapter:
    return LiteLLMAdapter(context, MANIFEST)

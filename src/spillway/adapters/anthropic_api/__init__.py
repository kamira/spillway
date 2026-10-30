"""anthropic_api:以 API key 經 LiteLLM 呼叫 Anthropic Messages API(spec 7.2、7.6.9)。"""
from __future__ import annotations

import copy

from spillway.adapters.base import API_DEFAULT_SIGNALS, AdapterContext, AdapterManifest

from .adapter import LiteLLMAdapter

# spec 5.3.1 api(anthropic)列:api 列之外,另加真實 Anthropic 的額度用完(HTTP 400 且訊息含
# `credit balance`)與過載(HTTP 529)訊號(決策 27)
ANTHROPIC_DEFAULT_SIGNALS = copy.deepcopy(API_DEFAULT_SIGNALS)
ANTHROPIC_DEFAULT_SIGNALS["quota"].append({"http_status": 400, "match": {"message": "credit balance"}})
ANTHROPIC_DEFAULT_SIGNALS["rate_limit"].append({"http_status": 529})

MANIFEST = AdapterManifest(
    providers=("anthropic",),
    kinds=("api",),
    access=("api",),
    cardinality="multiple",
    effort_map={"none": None, "low": "low", "medium": "medium", "high": "high", "max": "max"},
    default_signals=copy.deepcopy(ANTHROPIC_DEFAULT_SIGNALS),
    default_capabilities={"ctx": 200000, "tools": True, "vision": True,
                          "efforts": ["low", "medium", "high", "max"]},
    default_params={"timeout": 120},
    requires=(),
    contract_version=1,
)


def create(context: AdapterContext) -> LiteLLMAdapter:
    return LiteLLMAdapter(context, MANIFEST)

"""litellm:通用後備模組——沒有任何模組宣告的 `api`、`local` provider 都由它經 LiteLLM 呼叫
(spec 7.1.1、7.6.9)。

`providers` 只宣告 `litellm`(manifest 不允許空清單):provider 為 `litellm` 的成員,`model` 欄
直接寫完整的 LiteLLM 模型字串(例如 `ollama/llama3`)。其他 provider(`deepseek`、`ollama`、
`hosted_vllm`…)不在這裡宣告,經 `fallback_for_kinds` 落到本模組;日後新增專屬模組不會與本模組衝突。
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import urllib.request
from typing import Any

from spillway.adapters.base import API_DEFAULT_SIGNALS, AdapterContext, AdapterManifest

from .adapter import LiteLLMAdapter

MANIFEST = AdapterManifest(
    providers=("litellm",),
    kinds=("api", "local"),
    access=("api", "local"),
    cardinality="multiple",
    effort_map={"none": None, "low": "low", "medium": "medium", "high": "high", "max": "max"},
    default_signals=copy.deepcopy(API_DEFAULT_SIGNALS),
    default_capabilities={"ctx": 32768, "tools": False, "vision": False, "efforts": []},
    default_params={"timeout": 120},
    requires=(),
    contract_version=1,
    fallback_for_kinds=("api", "local"),
)

LOCAL_API_BASE_ENV = "SPILLWAY_LOCAL_API_BASE"
DEFAULT_LOCAL_API_BASE = "http://127.0.0.1:11434/v1"   # Ollama 的 OpenAI 相容端點
LIST_TIMEOUT = 2.0


def create(context: AdapterContext) -> LiteLLMAdapter:
    return LiteLLMAdapter(context, MANIFEST)


def _models_url(api_base: str) -> str:
    base = api_base.rstrip("/")
    return f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:   # 不跟隨轉址
        return None


def _fetch_json(url: str, timeout: float) -> Any:
    # 不讀環境的 proxy 設定、不跟隨轉址:local 查詢不可被送往他處(同 probe_http)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def list_models(context: AdapterContext, api_base: str | None = None) -> list[dict[str, Any]]:
    """查詢 local 端點的 OpenAI 相容 `/v1/models`,回傳 `[{"id": ...}, ...]`(spec 7.6.4)。

    端點依序取:參數 `api_base`、環境變數 `SPILLWAY_LOCAL_API_BASE`、Ollama 預設
    `http://127.0.0.1:11434/v1`。`api_base` 結尾有無 `/v1` 皆可。失敗時丟例外,由
    `adapters.base.available_models()` 改回只有 `models.yaml` 並附原因。
    """
    base = api_base or os.environ.get(LOCAL_API_BASE_ENV) or DEFAULT_LOCAL_API_BASE
    url = _models_url(base)
    if not url.startswith(("http://", "https://")):
        raise ValueError("local 端點必須是 http:// 或 https:// URL")
    data = await asyncio.to_thread(_fetch_json, url, LIST_TIMEOUT)
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("回應不是 OpenAI /v1/models 格式(缺少 data 清單)")
    return [{"id": m["id"]} for m in items
            if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]]

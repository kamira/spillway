"""經 LiteLLM 呼叫上游的 adapter(spec 7.1、7.2;決策 12、13)。

anthropic_api、openai_api、litellm 三個模組的 `adapter.py` 內容相同:模組之間不可互相 import
(design「Adapter 模組契約」規則 2),共用的只有 `adapters.base`。差異全部在各模組的 `MANIFEST`。

`call()` 流程:
1. 成員 `kind` 不在 manifest 的 `kinds` → `error`。
2. `resolve_signals(default_signals, dep.params)`;規則不合法 → `error`。
3. `effort_to_upstream()`;`none` 或 None → 不送 `reasoning_effort`。
4. 模型字串經 `adapters.base.model_string()`(spec 7.2.1):provider 為 `litellm` 時 `model`
   本身須已是 `<provider>/<model>`,原樣傳入;其他 provider 一律補 `<provider>/`。
5. 憑證:`credential_ref` 有值 → 經 `context.credentials` 解析並明確傳入;為空時 api 成員回
   `error` 且不發請求,local 成員以 `api_key=None` 呼叫(決策 13)。
6. `litellm_call`(`params.api_base`、`params.timeout`,預設 120 秒)。
7. 成功:local 成員 `cost_usd` 為 0(spec 5.2.4),api 成員用 `completion_cost()`,無定價為 None
   (spec 5.2.1);`tool_calls`、`finish_reason` 原樣帶出(spec 7.1;只回工具呼叫時 text 為空字串)。
   失敗:以 `classify()` 歸類;`error` 為 `litellm_call` 已遮罩金鑰的訊息。
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from spillway.adapters.base import (
    AdapterContext,
    AdapterManifest,
    CallResult,
    ChatRequest,
    Deployment,
    SignalsError,
    classify,
    effort_to_upstream,
    litellm_call,
    model_string,
    probe_http,
    resolve_signals,
)

def _error(message: str) -> CallResult:
    return CallResult(ok=False, text="", tokens_in=None, tokens_out=None, cost_usd=None,
                      status="error", error=message)


def _code_in_message(message: str) -> str | None:
    """從錯誤訊息內嵌的 JSON body 取出錯誤碼(`error.code`,其次 `error.type`)。

    `litellm_call` 只在例外帶 `body` 或訊息含 `"code": ...` 時取得 `error_code`;Anthropic
    協定的錯誤 body 是 `{"type":"error","error":{"type":"insufficient_quota",...}}`,沒有 `code`,
    LiteLLM 只把它放進訊息字串。不補這一步,`insufficient_quota` 會被當成一般 429。
    """
    start = message.find("{")
    if start < 0:
        return None
    try:
        body, _ = json.JSONDecoder().raw_decode(message, start)
    except ValueError:
        return None
    err = body.get("error") if isinstance(body, Mapping) else None
    if isinstance(err, Mapping):
        for key in ("code", "type"):
            v = err.get(key)
            if isinstance(v, str) and v and not v.isdigit():
                return v
    return None


def _enrich(raw: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(raw)
    if not out.get("error_code"):
        out["error_code"] = _code_in_message(str(out.get("message") or ""))
    return out


class LiteLLMAdapter:
    def __init__(self, context: AdapterContext, manifest: AdapterManifest) -> None:
        self.context = context
        self.manifest = manifest

    def _params(self, dep: Deployment) -> dict[str, Any]:
        return {**self.manifest.default_params, **(dep.params or {})}

    async def call(self, dep: Deployment, req: ChatRequest, effort: str | None) -> CallResult:
        if dep.kind not in self.manifest.kinds:
            return _error(f"此模組不支援 kind {dep.kind!r}")
        params = self._params(dep)
        try:
            signals = resolve_signals(self.manifest.default_signals, dep.params)
        except SignalsError as exc:
            return _error(f"params.signals 不合法:{exc}")
        try:
            upstream_effort = effort_to_upstream(self.manifest, effort)
            model = model_string(dep)
        except ValueError as exc:
            return _error(str(exc))
        try:
            timeout = float(params["timeout"]) if params.get("timeout") is not None else None
        except (TypeError, ValueError):
            return _error("params.timeout 必須是數字(秒)")

        ref = (dep.credential_ref or "").strip()
        if ref:
            try:
                api_key: str | None = self.context.credentials(ref)
            except Exception as exc:  # noqa: BLE001 — 解析失敗只回報原因,不發請求
                return _error(f"credential_ref 無法解析:{type(exc).__name__}: {exc}")
        elif dep.kind == "local":
            api_key = None
        else:
            return _error("api 成員未設定 credential_ref,未發出請求")

        result = await litellm_call(
            model=model,
            messages=req.messages,
            api_key=api_key,
            api_base=params.get("api_base") or None,
            timeout=timeout,
            reasoning_effort=upstream_effort,
            max_tokens=req.max_tokens,
            tools=req.tools,
            extra=req.extra,
        )
        if result.ok:
            cost = 0.0 if dep.kind == "local" else result.cost_usd
            return CallResult(ok=True, text=result.text, tokens_in=result.tokens_in,
                              tokens_out=result.tokens_out, cost_usd=cost, status="ok", error=None,
                              tool_calls=result.tool_calls, finish_reason=result.finish_reason)
        raw = _enrich(result.raw or {})
        return CallResult(ok=False, text="", tokens_in=None, tokens_out=None, cost_usd=None,
                          status=classify(raw, signals),
                          error=str(raw.get("message") or "上游呼叫失敗"))

    async def health(self, dep: Deployment) -> bool:
        """local:對 `params.api_base` 發 GET,2 秒內有任何 HTTP 回應即健康(決策 10);
        沒有 `api_base` 無從探測,回 False。api:不探測、回 True——額度與限流由 `call()` 的
        status 與冷卻處理,探測雲端會多一次網路往返且需要金鑰。"""
        if dep.kind != "local":
            return True
        base = self._params(dep).get("api_base")
        if not base:
            return False
        return await probe_http(str(base))

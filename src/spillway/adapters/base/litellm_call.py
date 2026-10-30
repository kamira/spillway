"""共用 LiteLLM 呼叫(spec 7.2;決策 5、13;安全影響 c、e)。

anthropic_api、openai_api、litellm 模組只經這裡呼叫 LiteLLM,不直接 import `litellm`。

- 一律 `litellm.acompletion`(async)。
- `api_key` 為必填關鍵字參數,原樣明確傳入(None 只用於不需金鑰的 local 端點);
  模組應在 `credential_ref` 為空的 api 成員上直接回 `error`,不呼叫本函式。
- `api_base` 有值才傳;`timeout` 預設 120 秒(`params.timeout` 覆寫由模組傳入)。
- `max_retries=0`:失敗交給路由 fallback,不在 SDK 內重試。
- 每次呼叫前把 `litellm.set_verbose` 設 False、`suppress_debug_info` 設 True;
  不註冊任何 callback、不傳 `callbacks`。
- 例外不外拋:轉成 `LiteLLMResult(ok=False, raw=...)`,`raw` 交給 `classify()`;
  `raw["message"]` 中的 `api_key` 值(含去前後空白、JSON 跳脫後的形式)已替換為 `***`。其他遮罩(`sk-`、Bearer)由應用層 Redactor 負責。

失敗時 `raw` 的欄位固定為:`http_status`(int|None)、`error_code`(str|None)、
`message`(str)、`exception`(例外類別名)、`timeout`(bool)。
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

DEFAULT_TIMEOUT = 120.0
PROTECTED_KEYS = frozenset(
    {"model", "messages", "api_key", "api_base", "timeout", "stream", "max_retries",
     "callbacks", "verbose", "reasoning_effort", "max_tokens", "tools"}
)
_CODE_RE = re.compile(r"""['"]code['"]\s*:\s*['"]([A-Za-z_][\w.\-]*)['"]""")


@dataclass(frozen=True)
class LiteLLMResult:
    ok: bool
    text: str = ""
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    raw: dict[str, Any] | None = None                    # 失敗時描述上游輸出,交給 classify()
    response: Any = field(default=None, repr=False)      # 成功時 LiteLLM 的原始回應


def _quiet(litellm: Any) -> None:
    litellm.set_verbose = False
    litellm.suppress_debug_info = True
    if hasattr(litellm, "telemetry"):
        litellm.telemetry = False


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _int_or_none(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


_INT_RE = re.compile(r"[0-9]+")


def _strict_int(v: str) -> int | None:
    """只接受 ASCII 數字(`"²"` 之類 isdigit 為 True 的字元不算),失敗回 None 不外拋。"""
    v = v.strip()
    return int(v) if _INT_RE.fullmatch(v) else None


def _status_of(exc: BaseException) -> int | None:
    for attr in ("status_code", "http_status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
        if isinstance(v, str):
            parsed = _strict_int(v)
            if parsed is not None:
                return parsed
    resp = getattr(exc, "response", None)
    v = getattr(resp, "status_code", None)
    return v if isinstance(v, int) else None


def _code_from_body(body: Any) -> str | None:
    if isinstance(body, (bytes, str)):
        try:
            body = json.loads(body)
        except (ValueError, TypeError):
            return None
    if isinstance(body, Mapping):
        err = body.get("error")
        for source in (err if isinstance(err, Mapping) else None, body):
            if isinstance(source, Mapping):
                code = source.get("code") or source.get("type")
                if isinstance(code, str) and code and not code.isdigit():
                    return code
    return None


def _error_code(exc: BaseException, message: str) -> str | None:
    code = _code_from_body(getattr(exc, "body", None))
    if code:
        return code
    m = _CODE_RE.search(message)
    if m:
        return m.group(1)
    attr = getattr(exc, "code", None)
    if isinstance(attr, str) and attr and not attr.isdigit():
        return attr
    return None


def _secret_variants(secret: str | None) -> list[str]:
    """金鑰可能出現在上游回顯中的形式:原值、去前後空白、JSON 字串跳脫後的兩者。"""
    if not secret:
        return []
    stripped = secret.strip()
    variants = {secret, stripped, json.dumps(secret)[1:-1], json.dumps(stripped)[1:-1]}
    return sorted((v for v in variants if v), key=len, reverse=True)


def _scrub(text: str, secret: str | None) -> str:
    for v in _secret_variants(secret):   # 由長到短:避免短變體先替換後長變體只剩殘片
        text = text.replace(v, "***")
    return text


def exception_to_raw(exc: BaseException, api_key: str | None = None) -> dict[str, Any]:
    message = _scrub(str(exc) or type(exc).__name__, api_key)
    name = type(exc).__name__
    status = _status_of(exc)
    return {
        "http_status": status,
        "error_code": _error_code(exc, message),
        "message": message,
        "exception": name,
        "timeout": isinstance(exc, TimeoutError) or "Timeout" in name,
    }


async def litellm_call(
    *,
    model: str,
    messages: list[dict[str, Any]],
    api_key: str | None,
    api_base: str | None = None,
    timeout: float | None = None,
    reasoning_effort: str | None = None,
    max_tokens: int | None = None,
    tools: list[dict[str, Any]] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> LiteLLMResult:
    """呼叫 `litellm.acompletion` 一次。`model` 須為 `<provider>/<model>`(spec 7.2.1)。

    `reasoning_effort` 為已經 `effort_to_upstream()` 轉換的上游值;None 時不傳。
    `extra` 為其餘 OpenAI 參數(例如 temperature),不可覆寫上列受保護參數(會被略過)。
    """
    # 防線:任何進入點(不只 app lifespan)第一次 import litellm 前都改用本地價格表,不在 import 時連網
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm  # 延後載入:只 import adapters.base 不必付出 litellm 的載入成本

    _quiet(litellm)
    kwargs: dict[str, Any] = {
        k: v for k, v in (extra or {}).items() if k not in PROTECTED_KEYS
    }
    kwargs.update(
        model=model,
        messages=messages,
        api_key=api_key,
        timeout=DEFAULT_TIMEOUT if timeout is None else timeout,
        stream=False,
        max_retries=0,
    )
    if api_base:
        kwargs["api_base"] = api_base
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if tools:
        kwargs["tools"] = tools

    try:
        resp = await litellm.acompletion(**kwargs)
    except Exception as exc:  # noqa: BLE001 — 例外一律轉成 raw,不外拋
        return LiteLLMResult(ok=False, raw=exception_to_raw(exc, api_key))

    choices = _get(resp, "choices") or []
    message = _get(choices[0], "message") if choices else None
    text = _get(message, "content") or ""
    usage = _get(resp, "usage")
    try:
        cost = litellm.completion_cost(completion_response=resp)
        cost = float(cost) if cost is not None else None
    except Exception:  # noqa: BLE001 — 無定價模型 → None(spec 5.4.4)
        cost = None
    return LiteLLMResult(
        ok=True,
        text=text if isinstance(text, str) else str(text),
        tokens_in=_int_or_none(_get(usage, "prompt_tokens")),
        tokens_out=_int_or_none(_get(usage, "completion_tokens")),
        cost_usd=cost,
        response=resp,
    )

"""共用 LiteLLM 呼叫(spec 7.2;決策 5、13;安全影響 c、e)。

anthropic_api、openai_api、litellm 模組只經這裡呼叫 LiteLLM,不直接 import `litellm`。

- 一律 `litellm.acompletion`(async)。
- `api_key` 為必填關鍵字參數,原樣明確傳入(None 只用於不需金鑰的 local 端點);
  模組應在 `credential_ref` 為空的 api 成員上直接回 `error`,不呼叫本函式。
- `api_base` 有值才傳;`timeout` 預設 120 秒(`params.timeout` 覆寫由模組傳入)。
- `max_retries=0`:失敗交給路由 fallback,不在 SDK 內重試。
- LiteLLM 一律經 `load_litellm()` 載入:先設 `LITELLM_LOCAL_MODEL_COST_MAP=True`(本地價格表),
  再把 `set_verbose` 設 False、`suppress_debug_info` 設 True、關閉遙測,並設
  `disable_hf_tokenizer_download=True`(token_counter 遇到 llama 系模型名不從 HuggingFace 下載
  tokenizer,退回 tiktoken;CHG-20261030-02 M13)。應用層的 `estimate` 也用同一個函式。
  不註冊任何 callback、不傳 `callbacks`。
- 模型字串一律經 `model_string(dep)`(spec 7.2.1;M11、M14):provider 為 `litellm`(直通)時原樣
  使用 `dep.model`(必須含 `/`),其他 provider 一律 `<provider>/<model>`,即使 model 已以
  `<provider>/` 開頭也照補(例如 vLLM 以 HF id `openai/gpt-oss-20b` 對外提供的模型)。
- 成功時帶出 `tool_calls`(OpenAI 格式 dict 清單;沒有時 None)與 `finish_reason`(spec 7.1;決策 28)。
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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from spillway.domain import Deployment

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
    tool_calls: list[dict[str, Any]] | None = None       # OpenAI 格式;沒有時 None
    finish_reason: str | None = None


PASSTHROUGH_PROVIDER = "litellm"
"""provider 為此值的成員:`model` 已是完整的 LiteLLM 模型字串(例如 `ollama/llama3`)。"""


def model_string(dep: "Deployment") -> str:
    """成員的 LiteLLM 模型字串(spec 7.2.1)。adapter 呼叫與 estimate 查價共用這一條規則。

    - provider 為 `litellm`(直通):原樣使用 `dep.model`,必須寫成 `<provider>/<model>`,
      否則丟 `ValueError`。
    - 其他 provider:一律 `f"{provider}/{model}"`;model 已以 `<provider>/` 開頭也照補
      (`openai` + `openai/gpt-oss-20b` → `openai/openai/gpt-oss-20b`,LiteLLM 剝掉第一段後
      上游收到 `openai/gpt-oss-20b`)。
    - model 為空丟 `ValueError`。
    """
    model = (dep.model or "").strip()
    if dep.provider == PASSTHROUGH_PROVIDER:
        head, sep, tail = model.partition("/")
        if not (head and sep and tail):
            raise ValueError(f"provider {PASSTHROUGH_PROVIDER} 的 model 必須寫成 <provider>/<model>")
        return model
    if not model:
        raise ValueError("model 不可為空")
    return f"{dep.provider}/{model}"


def load_litellm() -> Any:
    """載入並設定 LiteLLM(adapters.base 與應用層 estimate 共用的唯一入口)。

    每次呼叫都重設下列旗標(便宜,且防止其他程式碼改回):本地價格表、不 verbose、
    不印 debug 資訊、不送遙測、不從 HuggingFace 下載 tokenizer。
    """
    # 防線:任何進入點第一次 import litellm 前都改用本地價格表,不在 import 時連網
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm  # 延後載入:只 import adapters.base 不必付出 litellm 的載入成本

    litellm.set_verbose = False
    litellm.suppress_debug_info = True
    if hasattr(litellm, "telemetry"):
        litellm.telemetry = False
    litellm.disable_hf_tokenizer_download = True
    return litellm


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _tool_calls(items: Any) -> list[dict[str, Any]] | None:
    """LiteLLM 回應的 `message.tool_calls` 轉成 OpenAI 格式的 dict 清單;沒有或空清單時 None。"""
    if not items:
        return None
    out: list[dict[str, Any]] = []
    for tc in items:
        fn = _get(tc, "function")
        args = _get(fn, "arguments")
        if args is not None and not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        out.append({
            "id": _get(tc, "id"),
            "type": _get(tc, "type") or "function",
            "function": {"name": _get(fn, "name"), "arguments": args if args is not None else ""},
        })
    return out or None


def _int_or_none(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


_INT_RE = re.compile(r"[0-9]+")


def _strict_int(v: str) -> int | None:
    """只接受 ASCII 數字(`"²"` 之類 isdigit 為 True 的字元不算),失敗回 None 不外拋。"""
    v = v.strip()
    return int(v) if _INT_RE.fullmatch(v) else None


_CHAIN_LIMIT = 8


def _status_of(exc: BaseException) -> int | None:
    """上游實際回的 HTTP 狀態碼。

    LiteLLM 會改寫部分狀態碼:Anthropic 的 529(overloaded)被包成 `InternalServerError`,
    `status_code` 寫死為 500,原值只留在例外鏈(`__cause__`/`__context__`)裡的 provider 例外與
    httpx 例外上。因此沿例外鏈往下找,取最內層(最接近 HTTP 回應)的有效狀態碼;鏈上都沒有時
    用最外層例外自己的值。spec 5.3.1 anthropic 列的 529 規則靠這一步才能命中。
    """
    found: int | None = None
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen and len(seen) < _CHAIN_LIMIT:
        seen.add(id(cur))
        status = _own_status(cur)
        if status is not None and 100 <= status <= 599:
            found = status
        cur = cur.__cause__ or cur.__context__
    return found if found is not None else _own_status(exc)


def _own_status(exc: BaseException) -> int | None:
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
    litellm = load_litellm()
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
    first = choices[0] if choices else None
    message = _get(first, "message")
    text = _get(message, "content") or ""
    finish = _get(first, "finish_reason")
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
        tool_calls=_tool_calls(_get(message, "tool_calls")),
        finish_reason=finish if isinstance(finish, str) and finish else None,
    )

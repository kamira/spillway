"""HTTP 介面層(spec 9;CHG-20261030-02 T13 擁有)。

- `spillway.api.openai`:`/v1/*`(OpenAI 相容,T13)。
- `spillway.api.admin`:`/api/*` 管理端點(T14;由 `app.py` 掛載,T15 追加)。

本檔提供 spec 9.1.2 的錯誤格式工具(`/v1/*` 與 `/api/*` 共用同一份),`app.py` 以
`install_error_handlers(app)` 套到整個 app:

- `error_response(status, code, message)` → `{"error": {"code", "message"}}` 的 JSONResponse。
- `ApiError(status, code, message)`:端點內丟出即轉成上述格式。
- `RequestValidationError` → 422 `validation_error`,message 為「欄位路徑: 原因」以「; 」串接
  (loc 為空時只輸出原因);不回顯輸入值(可能是誤貼的金鑰),路徑中的鍵名另經 Redactor 遮罩。
- Starlette 的 HTTPException(未知路徑、方法不允許等)→ 同一格式:404 `not_found`、
  405 `method_not_allowed`,其餘狀態碼 `http_error`。
- 其他未處理例外 → 500 `internal_error`;日誌與 message 只含例外型別,不含例外內容。

另提供 `filter_chat_params(extra)`:OpenAI 請求 body 中「未列為模型欄位」的參數只轉交
`ALLOWED_CHAT_PARAMS` 白名單內的 OpenAI 標準參數,其餘鍵**靜默丟棄**(U8 = a)。這擋住
`base_url`、`api_base`、`custom_llm_provider`、`mock_response`、`extra_headers` 等可改變上游
目的地、協定或繞過上游的 LiteLLM 參數(否則成員的真實金鑰可能被送往任意主機)。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from spillway.redact import Redactor

__all__ = [
    "ALLOWED_CHAT_PARAMS",
    "ApiError",
    "error_response",
    "filter_chat_params",
    "install_error_handlers",
    "internal_error_response",
    "redact_echo",
    "validation_message",
]

_log = logging.getLogger(__name__)
_REDACTOR = Redactor()

_HTTP_CODES = {404: "not_found", 405: "method_not_allowed"}

# U8(使用者決定 2026-10-30):只轉交這些 OpenAI 標準參數;名單外的鍵靜默丟棄
ALLOWED_CHAT_PARAMS = frozenset({
    "temperature", "top_p", "n", "stop", "presence_penalty", "frequency_penalty",
    "logit_bias", "user", "response_format", "seed", "tool_choice", "parallel_tool_calls",
    "logprobs", "top_logprobs", "max_completion_tokens",
})


def filter_chat_params(extra: Mapping[str, Any] | None) -> dict[str, Any]:
    """`model_extra` → 只含 `ALLOWED_CHAT_PARAMS` 的新 dict(名單外的鍵丟棄,不報錯)。"""
    return {k: v for k, v in (extra or {}).items() if k in ALLOWED_CHAT_PARAMS}


def redact_echo(text: object) -> str:
    """錯誤訊息中回顯的使用者輸入(model 名、群組名、成員 id、鍵名)先以預設樣式遮罩。"""
    out, _ = _REDACTOR.redact(str(text))
    return out


class ApiError(Exception):
    """端點內丟出,由 handler 轉成 spec 9.1.2 格式。`message` 不可含秘密或請求內容。"""

    def __init__(self, status: int, code: str, message: str,
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers


def error_response(status: int, code: str, message: str,
                   headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers=headers)


def _loc(loc: Any) -> str:
    text = ".".join(str(p) for p in loc) if isinstance(loc, (list, tuple)) else str(loc)
    return redact_echo(text)


def validation_message(errors: list[Mapping[str, Any]]) -> str:
    """Pydantic 錯誤清單 → 「body.messages.0.role: Field required; …」;只用 loc 與 msg,不含 input。

    loc 為空時只輸出 msg;loc 內的鍵名(可能是使用者送的未知鍵)經 Redactor 遮罩。
    """
    parts = []
    for e in errors:
        loc = _loc(e.get("loc", ()))
        msg = str(e.get("msg", "invalid"))
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) or "請求格式錯誤"


def internal_error_response(exc: BaseException) -> JSONResponse:
    """非預期例外 → 500 `internal_error`;日誌與 message 只含例外型別。"""
    name = type(exc).__name__
    _log.error("未處理的內部錯誤:%s", name)
    return error_response(500, "internal_error", f"內部錯誤({name})")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(422, "validation_error", validation_message(list(exc.errors())))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "http_error")
        detail = exc.detail if isinstance(exc.detail, str) and exc.detail else code
        return error_response(exc.status_code, code, detail, getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        return internal_error_response(exc)

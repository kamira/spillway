"""HTTP 介面層(spec 9;CHG-20261030-02 T13 擁有)。

- `spillway.api.openai`:`/v1/*`(OpenAI 相容,T13)。
- `spillway.api.admin`:`/api/*` 管理端點(T14;由 `app.py` 掛載,T15 追加)。

本檔提供 spec 9.1.2 的錯誤格式工具,`app.py` 以 `install_error_handlers(app)` 套到整個 app:

- `error_response(status, code, message)` → `{"error": {"code", "message"}}` 的 JSONResponse。
- `ApiError(status, code, message)`:端點內丟出即轉成上述格式。
- `RequestValidationError` → 422 `validation_error`,message 為「欄位路徑: 原因」以「; 」串接;
  不回顯輸入值(可能是誤貼的金鑰)。
- Starlette 的 HTTPException(未知路徑、方法不允許等)→ 同一格式:404 `not_found`、
  405 `method_not_allowed`,其餘狀態碼 `http_error`。
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

__all__ = [
    "ApiError",
    "error_response",
    "install_error_handlers",
    "validation_message",
]

_HTTP_CODES = {404: "not_found", 405: "method_not_allowed"}


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
    return ".".join(str(p) for p in loc) if isinstance(loc, (list, tuple)) else str(loc)


def validation_message(errors: list[dict[str, Any]]) -> str:
    """Pydantic 錯誤清單 → 「body.messages.0.role: Field required; …」;只用 loc 與 msg,不含 input。"""
    parts = [f"{_loc(e.get('loc', ()))}: {e.get('msg', 'invalid')}" for e in errors]
    return "; ".join(parts) or "請求格式錯誤"


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

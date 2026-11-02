"""管理 API 共用:錯誤工具、`_AdminRoute`、`router` 本體與共用 helper(CHG-20261101-02 決策 9)。

各子模組以本模組的 `router` 註冊端點;`spillway.api.admin.__init__` 依原檔順序匯入子模組。
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from spillway.api import (
    ApiError,
    error_response,
    redact_echo,
    validation_message,
)
from spillway.clock import to_iso
from spillway.estimate import Estimate
from spillway.redact import REDACTOR


_REDACTOR = REDACTOR


# ================================================================ 錯誤處理(共用 spillway.api 的工具)


def _invalid(path: str, message: str) -> ApiError:
    return ApiError(422, "validation_error", f"{path}: {message}")


def _not_found(what: str) -> ApiError:
    """`what` 可能含使用者輸入(id、名稱),先經 Redactor;英數開頭時與「找不到」之間補空白。"""
    what = redact_echo(what.strip())
    sep = " " if what[:1].isascii() and what[:1].isalnum() else ""
    return ApiError(404, "not_found", f"找不到{sep}{what}")


class _AdminRoute(APIRoute):
    """把本 router 內的 `RequestValidationError` 與 `ApiError` 轉成 9.1.2 格式。

    exception handler 只能掛在 app 上;以 route class 處理,admin.router 掛到任何 app 都一致。
    轉換一律呼叫 `spillway.api` 的共用函式(與 app 層 handler 同一份格式)。
    """

    def get_route_handler(self) -> Callable[[Request], Any]:
        handler = super().get_route_handler()

        async def wrapped(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                return error_response(422, "validation_error", validation_message(list(exc.errors())))
            except ApiError as exc:
                return error_response(exc.status, exc.code, exc.message, exc.headers)

        return wrapped


router = APIRouter(prefix="/api", route_class=_AdminRoute)


# ================================================================ 共用


def _clock(request: Request) -> Any:
    return request.app.state.clock


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else to_iso(dt)


def _estimate_out(est: Estimate | None) -> dict[str, Any] | None:
    return None if est is None else dataclasses.asdict(est)


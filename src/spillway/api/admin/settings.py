"""`/api/settings/context`(CHG-20261101-02 決策 9)與 `/api/settings/token/rotate`
(CHG-20261101-03 T7 決策 14)。"""
from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from spillway import settings as spillway_settings
from spillway.api.deps import request_conn
from spillway.store import repo
from spillway.store.db import transaction

from .common import router


CONTEXT_MAX_BYTES_MIN = 1024     # context_max_bytes 下限:小於 1 KiB 的 payload 已無檢視價值
CONTEXT_RETENTION_DAYS_MAX = 36500   # 約 100 年;再大會讓 ledger._cutoff 溢位(審議 M25)


# ================================================================ /api/settings/context


class ContextSettingsIn(BaseModel):
    """部分更新:未給或為 null 的鍵不變。`context_retention_days` 0–36500(0 = 不保存內容,spec 2.4.5;
    上限 `CONTEXT_RETENTION_DAYS_MAX`,避免清理的截止時間溢位而永遠刪不掉);
    `context_max_bytes` ≥ 1024(`CONTEXT_MAX_BYTES_MIN`)。型別嚴格(不接受字串數字、1/0 當布林)。"""

    model_config = ConfigDict(extra="forbid")

    record_context_default: StrictBool | None = None
    context_retention_days: StrictInt | None = Field(default=None, ge=0,
                                                     le=CONTEXT_RETENTION_DAYS_MAX)
    context_max_bytes: StrictInt | None = Field(default=None, ge=CONTEXT_MAX_BYTES_MIN)


def _context_out(conn: sqlite3.Connection) -> dict[str, Any]:
    s = repo.get_context_settings(conn)
    return {"record_context_default": s.record_default, "context_retention_days": s.retention_days,
            "context_max_bytes": s.max_bytes}


@router.get("/settings/context")
async def get_context_settings(request: Request) -> dict[str, Any]:
    """`{record_context_default: bool, context_retention_days: int, context_max_bytes: int}`;
    資料庫缺鍵時為常數預設值(決策 24),讀取不寫入資料列。"""
    with request_conn(request) as conn:
        return _context_out(conn)


@router.put("/settings/context")
async def put_context_settings(request: Request, body: ContextSettingsIn) -> dict[str, Any]:
    """部分更新(同一交易);回更新後的三個值。"""
    with request_conn(request) as conn:
        with transaction(conn):
            for key, value in body.model_dump(exclude_none=True).items():
                repo.set_setting(conn, key, value)
        return _context_out(conn)


# ================================================================ /api/settings/token/rotate


@router.post("/settings/token/rotate", response_model=None)
async def rotate_access_token(request: Request) -> JSONResponse:
    """重新產生存取 token:資料庫只存 SHA-256,回應 `{"token": "..."}` 是明文唯一一次出現
    (決策 14;`Cache-Control: no-store`)。不讀 body、不寫帳本、不記錄 token。"""
    with request_conn(request) as conn:
        token = spillway_settings.rotate_access_token(conn)
    return JSONResponse({"token": token}, headers={"Cache-Control": "no-store"})

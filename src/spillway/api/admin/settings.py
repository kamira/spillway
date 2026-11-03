"""`/api/settings/context`(CHG-20261101-02 決策 9)、`/api/settings/token/rotate`
(CHG-20261101-03 T7 決策 14)與 `/api/settings` 設定總覽(CHG-20261102-02 決策 10、19)。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
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


# ================================================================ /api/settings(設定總覽)


# 版本來源單一定義在 `spillway.settings.package_version`(CHG-20261103-01 決策 21 ③);
# 保留模組內名稱 `_version` 供既有測試引用
_version = spillway_settings.package_version


@router.get("/settings")
async def get_settings_overview(request: Request) -> dict[str, Any]:
    """設定總覽(spec 9.2.7;決策 10、19),供 UI-7 與首次設定精靈:

    `{port, data_dir, version, token_issued, first_run,
      counts: {deployments, groups, routes, ledger_ok},
      context: {record_context_default, context_retention_days, context_max_bytes,
                payload_count, payload_bytes}}`

    - `port`:serve 綁定的埠(`app.state.port`;唯讀、顯示用;不含綁定位址)。
    - `data_dir`:`Path(app.state.db_path).parent`(與備份目錄同源;不讀 `settings.data_dir()`)。
    - `token_issued`:存取 token 雜湊是否已存在;不回 token 或雜湊。
    - `first_run`:`counts.deployments == 0 and counts.routes == 0`(replace 匯入空檔案後會再次為 true)。
    - `counts.ledger_ok`:`status = 'ok'` 的帳本列數;`payload_bytes` 為 `usage_payloads.bytes` 加總。
    """
    state = request.app.state
    with request_conn(request) as conn:
        counts = repo.count_config(conn)
        payload_count, payload_bytes = repo.payload_stats(conn)
        token_issued = spillway_settings.get_access_token_hash(conn) is not None
        context = _context_out(conn)
    return {
        "port": state.port,
        "data_dir": str(Path(state.db_path).parent),
        "version": spillway_settings.package_version(),
        "token_issued": token_issued,
        "first_run": counts["deployments"] == 0 and counts["routes"] == 0,
        "counts": counts,
        "context": {**context, "payload_count": payload_count, "payload_bytes": payload_bytes},
    }


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

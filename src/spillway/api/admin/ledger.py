"""`/api/ledger` 與 payload 檢視(CHG-20261101-02 決策 9)。"""
from __future__ import annotations

import dataclasses
import math
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import Query, Request

from spillway.api import ApiError
from spillway.api.deps import request_conn
from spillway.clock import to_iso
from spillway.domain import LEDGER_STATUSES
from spillway.ledger import LedgerNotFound, payload_status
from spillway.store import repo

from .common import _clock, _invalid, _not_found, router


LEDGER_PAGE_SIZE = 50
LEDGER_PAGE_SIZE_MAX = 500
LEDGER_PAGE_MAX = 1_000_000      # S2:offset 留在 SQLite int64 內;超過回 422


# ================================================================ /api/ledger


def _parse_time(value: str | None, path: str) -> datetime | None:
    """ISO 8601(`2026-10-30T12:00:00Z`、`+00:00`)或日期(`2026-10-30` = 當日 00:00);
    沒有時區時視為 UTC。"""
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise _invalid(path, "必須是 ISO 8601 時間(例:2026-10-30T12:00:00Z)") from None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _ledger_out(r: repo.LedgerRow) -> dict[str, Any]:
    d = dataclasses.asdict(r)
    d["ts"] = to_iso(r.ts)
    return {"id": d.pop("id"), **d}


# 須與 domain.LEDGER_STATUSES 一致,由 tests/unit/test_domain_constants.py 釘住(決策 6)
StatusLit = Literal["ok", "error", "quota_exhausted", "rate_limited", "skipped_precheck"]
assert set(StatusLit.__args__) == LEDGER_STATUSES  # type: ignore[attr-defined]


@router.get("/ledger")
async def list_ledger(
    request: Request,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    deployment: str | None = None,
    route: str | None = None,
    status: StatusLit | None = None,
    page: Annotated[int, Query(ge=1, le=LEDGER_PAGE_MAX)] = 1,
    page_size: Annotated[int, Query(ge=1, le=LEDGER_PAGE_SIZE_MAX)] = LEDGER_PAGE_SIZE,
) -> dict[str, Any]:
    """帳本查詢(spec 9.2)。

    參數:`from`(含)、`to`(不含)為 ISO 8601 時間或日期(無時區視為 UTC);`deployment`、`route`、
    `status` 為等值篩選;`page` 自 1 起、上限 1,000,000(超過回 422);`page_size` 預設 50、上限 500(本筆另加的可選參數)。

    分頁格式:`{items: [帳本列…], page, page_size, total, pages}`;`items` 依 `ts DESC, id DESC`
    (新到舊);`total` 為符合條件的總列數,`pages = ceil(total / page_size)`(total 為 0 時 0);
    超過最後一頁時 `items` 為空。帳本列欄位同 `usage_ledger`(spec 2.3),`ts` 為 ISO 字串。
    """
    since = _parse_time(from_, "query.from")
    until = _parse_time(to, "query.to")
    with request_conn(request) as conn:
        rows = repo.list_ledger(conn, route_name=route, deployment_id=deployment, status=status,
                                since=since, until=until, limit=page_size,
                                offset=(page - 1) * page_size)
        # 清單與計數為兩條獨立敘述,不包交易(決策審議 D25)
        total = repo.count_ledger(conn, route_name=route, deployment_id=deployment, status=status,
                                  since=since, until=until)
    return {"items": [_ledger_out(r) for r in rows], "page": page, "page_size": page_size,
            "total": total, "pages": math.ceil(total / page_size)}


@router.get("/ledger/{ledger_id}/payload")
async def get_payload(request: Request, ledger_id: int) -> dict[str, Any]:
    """該次嘗試的請求與回應內容(spec 9.2.1;決策 17,依 `ledger.payload_status`)。

    200:`{ledger_id, ts, request_json, response_text, bytes, truncated, redacted}`(內容已於寫入時遮罩);
    已過保存期限 → 410 `payload_expired`;從未記錄或帳本列不存在 → 404 `not_found`。
    """
    with request_conn(request) as conn:
        try:
            st = payload_status(conn, ledger_id, _clock(request).now())
        except LedgerNotFound:
            raise _not_found(f"帳本列 {ledger_id}") from None
        if st == "expired":
            raise ApiError(410, "payload_expired", f"帳本列 {ledger_id} 的內容已過保存期限")
        p = repo.get_payload(conn, ledger_id) if st == "ok" else None
        if p is None:
            raise _not_found(f"帳本列 {ledger_id} 的內容(未記錄)")
    return {"ledger_id": p.ledger_id, "ts": to_iso(p.ts), "request_json": p.request_json,
            "response_text": p.response_text, "bytes": p.bytes, "truncated": p.truncated,
            "redacted": p.redacted}

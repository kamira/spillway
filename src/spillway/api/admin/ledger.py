"""`/api/ledger` 與 payload 檢視(CHG-20261101-02 決策 9);`/api/ledger/export.csv`
(CHG-20261102-02 決策 9)。"""
from __future__ import annotations

import asyncio
import csv
import dataclasses
import io
import math
from collections.abc import AsyncIterator, Iterable
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import Query, Request
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from spillway.api import ApiError
from spillway.api.deps import request_conn
from spillway.clock import to_iso, utc_stamp
from spillway.domain import LEDGER_STATUSES
from spillway.ledger import LedgerNotFound, payload_status
from spillway.store import repo

from .common import _clock, _not_found, invalid, router


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
        raise invalid(path, "必須是 ISO 8601 時間(例:2026-10-30T12:00:00Z)") from None
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


# ================================================================ /api/ledger/export.csv
# CHG-20261102-02 決策 9(決策審議 AM-10)


LEDGER_CSV_MAX_ROWS = 100_000
LEDGER_CSV_BATCH = 1000
# 欄位與順序同 `_ledger_out`(`id` 在前,其餘為 `repo.LedgerRow` 欄位順序)
LEDGER_CSV_COLUMNS = (
    "id", "ts", "route_name", "deployment_id", "effort", "tokens_in", "tokens_out", "cost_usd",
    "status", "slot", "fallback_depth", "latency_ms", "est_tokens_in", "est_tokens_out_p50",
    "est_tokens_out_p90", "est_cost_usd_p90", "error",
)
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_BOM = "\ufeff"


def _csv_cell(value: Any) -> Any:
    """None → 空字串;`str` 以 `= + - @ Tab CR` 開頭時前置 `'`(試算表公式注入防護);數字原樣。"""
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


def _csv_text(rows: Iterable[Iterable[Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    for row in rows:
        w.writerow([_csv_cell(v) for v in row])
    return buf.getvalue()


def _csv_row(r: repo.LedgerRow) -> list[Any]:
    out = _ledger_out(r)
    return [out[c] for c in LEDGER_CSV_COLUMNS]


class _ClosingStreamingResponse(StreamingResponse):
    """回應結束(含用戶端中途斷線、取消)時一定 `aclose()` body 的 async generator,
    讓 generator 內開的連線在事件迴圈執行緒內、立即關閉,不等垃圾回收。"""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.body_iterator.aclose()  # type: ignore[attr-defined]


@router.get("/ledger/export.csv", response_model=None,
            responses={200: {"content": {"text/csv": {}}, "description": "帳本 CSV(UTF-8 BOM)"}})
async def export_ledger_csv(
    request: Request,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    deployment: Annotated[str | None, Query()] = None,
    route: Annotated[str | None, Query()] = None,
    status: Annotated[StatusLit | None, Query()] = None,
) -> StreamingResponse:
    """帳本 CSV 匯出(spec 9.2.6;決策 9)。篩選參數同 `GET /api/ledger`(`from`、`to`、`deployment`、
    `route`、`status`),不分頁,依 `ts DESC, id DESC`;符合列數超過 100,000 → 422 `validation_error`
    (請縮小範圍)。

    欄位與順序同帳本列(`LEDGER_CSV_COLUMNS`);`ts` 為 UTC ISO 8601;None 為空字串;不含 payload。
    `str` 欄位以 `= + - @ Tab CR` 開頭時前置 `'`;數字欄不加。回應 `text/csv; charset=utf-8`、
    UTF-8 BOM、`Content-Disposition: attachment; filename="spillway-ledger-<UTC yyyymmddThhmmssZ>.csv"`
    (時間取自 clock)、`Cache-Control: no-store`、`X-Content-Type-Options: nosniff`。

    串流:預檢在 `request_conn` 內完成;CSV 由 async generator 在事件迴圈執行緒內自行以
    `conn_factory()` 開連線、`try/finally` 關閉(design D27 的 CSV 串流例外),以
    `repo.iter_ledger` 逐批輸出,批次之間讓出事件迴圈。
    """
    since = _parse_time(from_, "query.from")
    until = _parse_time(to, "query.to")
    filters: dict[str, Any] = {"route_name": route, "deployment_id": deployment, "status": status,
                               "since": since, "until": until}
    with request_conn(request) as conn:
        total = repo.count_ledger(conn, **filters)
    if total > LEDGER_CSV_MAX_ROWS:
        raise invalid("query", f"符合條件的帳本列有 {total} 列,超過上限 {LEDGER_CSV_MAX_ROWS} 列,"
                               "請縮小範圍")
    filename = f"spillway-ledger-{utc_stamp(_clock(request).now())}.csv"
    factory = request.app.state.conn_factory

    async def body() -> AsyncIterator[str]:
        conn = factory()
        try:
            yield _BOM + _csv_text([LEDGER_CSV_COLUMNS])
            batches = repo.iter_ledger(conn, **filters, limit=LEDGER_CSV_MAX_ROWS,
                                       batch=LEDGER_CSV_BATCH)
            try:
                for rows in batches:
                    yield _csv_text(_csv_row(r) for r in rows)
                    await asyncio.sleep(0)
            finally:
                batches.close()
        finally:
            conn.close()

    return _ClosingStreamingResponse(
        body(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                 "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


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

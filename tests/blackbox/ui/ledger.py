"""UI-6 帳本與 UI-1 總覽共用的種資料 helper(CHG-20261103-01 T4;決策 22)。

(CHG-20261107-03 T2 由 `tests/helpers/ui_ledger.py` 複製改寫成黑箱版;原檔不動,T5 改用本模組後刪除)
帳本列與 payload 沒有 `/api` 寫入端點,一律經測試控制介面 `POST /__test__/ledger`、`/__test__/payloads`
(spec 13.4.3;upsert,`id` 已存在時整列取代)寫入。時間以 `YYYY-MM-DDTHH:MM:SSZ` 送出(spec 2.1.2)。
種入不經遮罩;驗證遮罩的情境不得使用本檔。

- `insert_ledger(gw, ledger_id, ts, *, deployment, route="rt", status="ok", **cols)`:寫入一列帳本(指定 id)。
- `insert_payload(gw, ledger_id, ts, request_json, response_text=None, *, truncated=False, redacted=False)`。
- `bulk_ledger(gw, count, ts, *, deployment="bulk", route="rt", status="ok")`:分批(每批 `BULK_CHUNK` 列)寫入大量列。
- 網址 query 的 `query_of` 在 `tests.blackbox.ui`。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from tests.blackbox.control import iso

__all__ = ["BULK_CHUNK", "bulk_ledger", "insert_ledger", "insert_payload"]

BULK_CHUNK = 10_000

_COLS = ("route_name", "deployment_id", "effort", "tokens_in", "tokens_out", "cost_usd", "status", "slot",
         "fallback_depth", "latency_ms", "est_tokens_in", "est_tokens_out_p50", "est_tokens_out_p90",
         "est_cost_usd_p90", "error")


def insert_ledger(gw: Any, ledger_id: int, ts: datetime, *, deployment: str | None, route: str | None = "rt",
                  status: str = "ok", **cols: Any) -> int:
    values: dict[str, Any] = {"route_name": route, "deployment_id": deployment, "status": status,
                              "fallback_depth": 0}
    for k, v in cols.items():
        assert k in _COLS, k
        values[k] = v
    ids = gw.control.ledger([{"id": ledger_id, "ts": iso(ts), **values}])
    assert ids == [ledger_id], ids
    return ledger_id


def insert_payload(gw: Any, ledger_id: int, ts: datetime, request_json: str, response_text: str | None = None,
                   *, truncated: bool = False, redacted: bool = False) -> None:
    size = len(request_json.encode()) + len((response_text or "").encode())
    gw.control.payloads([{"ledger_id": ledger_id, "ts": iso(ts), "request_json": request_json,
                          "response_text": response_text, "bytes": size, "truncated": truncated,
                          "redacted": redacted}])


def bulk_ledger(gw: Any, count: int, ts: datetime, *, deployment: str = "bulk", route: str = "rt",
                status: str = "ok") -> None:
    row = {"ts": iso(ts), "route_name": route, "deployment_id": deployment, "status": status, "fallback_depth": 0}
    done = 0
    while done < count:
        n = min(BULK_CHUNK, count - done)
        assert len(gw.control.ledger([row] * n)) == n
        done += n

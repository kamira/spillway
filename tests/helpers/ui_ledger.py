"""UI-6 帳本與 UI-1 總覽共用的種資料 helper(CHG-20261103-01 T4;決策 22)。

帳本列與 payload 沒有 `/api` 寫入端點,一律經 `tests/helpers/ui.db_execute`(單一 DB adapter 層)寫入;
本檔只組 SQL 與參數。時間一律以 `spillway.clock.to_iso` 存成 UTC ISO(與 `repo.insert_ledger` 相同)。

- `insert_ledger(gw, ledger_id, ts, *, deployment, route="rt", status="ok", **cols)`:寫入一列帳本(指定 id)。
- `insert_payload(gw, ledger_id, ts, request_json, response_text=None, *, truncated=False, redacted=False)`。
- `bulk_ledger(gw, count, ts, *, deployment="bulk", route="rt", status="ok")`:以遞迴 CTE 一次寫入大量列。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from spillway.clock import to_iso
from tests.helpers.ui import db_execute

__all__ = ["bulk_ledger", "insert_ledger", "insert_payload"]

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
    names = ["id", "ts", *values]
    db_execute(gw, f"INSERT INTO usage_ledger ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
               [ledger_id, to_iso(ts), *values.values()])
    return ledger_id


def insert_payload(gw: Any, ledger_id: int, ts: datetime, request_json: str, response_text: str | None = None,
                   *, truncated: bool = False, redacted: bool = False) -> None:
    size = len(request_json.encode()) + len((response_text or "").encode())
    db_execute(gw, "INSERT INTO usage_payloads (ledger_id, ts, request_json, response_text, bytes, truncated, "
                   "redacted) VALUES (?, ?, ?, ?, ?, ?, ?)",
               [ledger_id, to_iso(ts), request_json, response_text, size, int(truncated), int(redacted)])


def bulk_ledger(gw: Any, count: int, ts: datetime, *, deployment: str = "bulk", route: str = "rt",
                status: str = "ok") -> None:
    db_execute(gw, "INSERT INTO usage_ledger (ts, route_name, deployment_id, status, fallback_depth) "
                   "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < ?) "
                   "SELECT ?, ?, ?, ?, 0 FROM c",
               [count, to_iso(ts), route, deployment, status])

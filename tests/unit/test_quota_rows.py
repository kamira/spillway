"""`spillway.quota.member_quota_rows`(CHG-20261102-01 決策 12;AM-12)。

dashboard 與 MCP `quota_status` 共用的推導:無上限、有上限(含負值與比例夾值)、U10 只記冷卻的暫行列、
冷卻到期與否、近 24 小時請求數、預估剩餘天數;查詢數與成員數無關。
既有 `/api/dashboard` 測試不改而全過,證明抽出前後回應相同。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from spillway.clock import FakeClock
from spillway.domain import Budget
from spillway.quota import is_cooling_only, member_quota_rows
from spillway.store import repo
from spillway.store.db import connect
from tests.helpers import api_member

NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "q.db")
    try:
        yield c
    finally:
        c.close()


def _spend(conn: sqlite3.Connection, dep: str, cost: float, ts: datetime, status: str = "ok") -> None:
    repo.insert_ledger(conn, repo.LedgerRow(ts=ts, status=status, deployment_id=dep, cost_usd=cost))


def test_empty(conn):
    assert member_quota_rows(conn, FakeClock(NOW), NOW) == []


def test_rows(conn):
    for i in ("a-unl", "b-usd", "c-over", "d-cool", "e-legacy"):
        repo.save_deployment(conn, api_member(i, "http://127.0.0.1:9"))
    repo.save_budget(conn, "b-usd", Budget(type="usd", limit_amount=10.0, window="monthly", reset_anchor=1))
    repo.save_budget(conn, "c-over", Budget(type="usd", limit_amount=1.0, window="none", reset_anchor=None))
    _spend(conn, "b-usd", 2.5, NOW - timedelta(hours=1))
    _spend(conn, "b-usd", 100.0, NOW - timedelta(days=40))          # 上期,不計
    _spend(conn, "c-over", 3.0, NOW - timedelta(days=400))          # window none:累計
    repo.mark_cooldown(conn, "d-cool", NOW + timedelta(minutes=5), "rate_limited")
    repo.mark_cooldown(conn, "e-legacy", NOW - timedelta(minutes=5), "quota_exhausted")   # 已到期
    conn.commit()

    rows = {r["id"]: r for r in member_quota_rows(conn, FakeClock(NOW), NOW)}
    assert list(rows) == ["a-unl", "b-usd", "c-over", "d-cool", "e-legacy"]

    a = rows["a-unl"]
    assert (a["unlimited"], a["remaining"], a["ratio"], a["budget_type"], a["window"]) == (True, None, 1.0, None, None)
    assert a["period_start"] == "2026-10-01T00:00:00Z" and a["next_reset"] == "2026-11-01T00:00:00Z"

    b = rows["b-usd"]
    assert b["unlimited"] is False and b["budget_type"] == "usd" and b["window"] == "monthly"
    assert b["period_spend"] == pytest.approx(2.5) and b["remaining"] == pytest.approx(7.5)
    assert b["ratio"] == pytest.approx(0.75) and b["requests_24h"] == 1
    assert b["est_days_left"] == pytest.approx(7.5 / (2.5 / 7))

    c = rows["c-over"]
    assert c["period_start"] is None and c["next_reset"] is None
    assert c["remaining"] == pytest.approx(-2.0) and c["ratio"] == 0.0

    d = rows["d-cool"]
    assert d["cooling"] is True and d["cooling_reason"] == "rate_limited"
    assert d["cooling_until"] == "2026-10-30T12:05:00Z"

    e = rows["e-legacy"]
    assert e["cooling"] is False and e["cooling_until"] is None and e["cooling_reason"] is None


def test_cooling_only_row_is_like_no_budget(conn):
    repo.save_deployment(conn, api_member("x", "http://127.0.0.1:9"))
    repo.save_budget(conn, "x", Budget(type="none", limit_amount=None, window="none", reset_anchor=None))
    conn.commit()
    assert is_cooling_only("none", "none", None) is True
    assert is_cooling_only("none", "monthly", None) is False
    (row,) = member_quota_rows(conn, FakeClock(NOW), NOW)
    assert row["budget_type"] is None and row["window"] is None and row["unlimited"] is True
    assert row["period_start"] == "2026-10-01T00:00:00Z"   # 比照沒有 budgets 列:日曆月


def test_query_count_independent_of_member_count(conn):
    def count() -> int:
        n = [0]
        conn.set_trace_callback(lambda _sql: n.__setitem__(0, n[0] + 1))
        try:
            member_quota_rows(conn, FakeClock(NOW), NOW)
        finally:
            conn.set_trace_callback(None)
        return n[0]

    repo.save_deployment(conn, api_member("m0", "http://127.0.0.1:9"))
    conn.commit()
    base = count()
    for i in range(1, 20):
        repo.save_deployment(conn, api_member(f"m{i}", "http://127.0.0.1:9"))
    conn.commit()
    assert count() == base


def test_daily_spend_7d_and_est_days_left_share_daily(conn):
    """CHG-20261103-01 決策 10 ③:`daily_spend_7d = spend7 / 7`,`est_days_left` 由同一個值推導。"""
    for i in ("new", "quiet", "pool"):
        repo.save_deployment(conn, api_member(i, "http://127.0.0.1:9"))
    repo.save_budget(conn, "quiet", Budget(type="usd", limit_amount=5.0, window="none", reset_anchor=None))
    repo.save_budget(conn, "pool", Budget(type="credit_pool", limit_amount=20.0, window="none",
                                          reset_anchor=None))
    _spend(conn, "quiet", 4.0, NOW - timedelta(days=7, seconds=1))   # 剛好超出 7 天
    _spend(conn, "pool", 0.3, NOW - timedelta(days=2))
    _spend(conn, "pool", 0.4, NOW - timedelta(days=7))               # 邊界(含)
    conn.commit()
    rows = {r["id"]: r for r in member_quota_rows(conn, FakeClock(NOW), NOW)}
    assert rows["new"]["daily_spend_7d"] == 0.0
    assert rows["quiet"]["daily_spend_7d"] == 0.0 and rows["quiet"]["est_days_left"] is None
    pool = rows["pool"]
    assert pool["daily_spend_7d"] == (0.3 + 0.4) / 7
    assert pool["est_days_left"] == max(pool["remaining"], 0.0) / pool["daily_spend_7d"]

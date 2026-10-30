"""QuotaService 單元測試(CHG-20261030-02 T7;spec 5.1、5.3.2、5.3.3、4.2.4、4.2.6、4.5.2;決策 6)。"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from spillway.clock import FakeClock
from spillway.domain import Budget, Deployment
from spillway.quota import QuotaService
from spillway.store import repo

UTC = timezone.utc


def T(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _dep(dep_id: str = "a1", budget: Budget | None = None) -> Deployment:
    return Deployment(
        id=dep_id, model_name="m", kind="api", provider="openai_api", model="gpt-x", budget=budget
    )


def _save(db: sqlite3.Connection, budget: Budget | None, dep_id: str = "a1") -> Deployment:
    dep = _dep(dep_id, budget)
    repo.save_deployment(db, dep)
    return dep


def _spend(db: sqlite3.Connection, dep_id: str, ts: datetime, cost: float | None, status: str = "ok") -> None:
    repo.insert_ledger(db, repo.LedgerRow(ts=ts, status=status, deployment_id=dep_id, cost_usd=cost))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T(2026, 10, 30, 12, 0, 0))


@pytest.fixture
def qs(db: sqlite3.Connection, clock: FakeClock) -> QuotaService:
    return QuotaService(db, clock)


# ---------------------------------------------------------------- L1:期間(5.1)


def test_monthly_anchor_1(qs: QuotaService) -> None:
    b = Budget(type="usd", limit_amount=10, window="monthly", reset_anchor=1)
    now = T(2026, 10, 30, 12)
    assert qs.period_start(b, now) == T(2026, 10, 1)
    assert qs.next_reset(b, now) == T(2026, 11, 1)


def test_monthly_anchor_28_not_reached_takes_previous_month(qs: QuotaService) -> None:
    b = Budget(type="usd", limit_amount=10, window="monthly", reset_anchor=28)
    now = T(2027, 2, 10, 9, 30)
    assert qs.period_start(b, now) == T(2027, 1, 28)
    assert qs.next_reset(b, now) == T(2027, 2, 28)


def test_monthly_anchor_day_itself_starts_new_period(qs: QuotaService) -> None:
    b = Budget(type="usd", limit_amount=10, window="monthly", reset_anchor=28)
    now = T(2027, 2, 28, 0, 0, 0)
    assert qs.period_start(b, now) == T(2027, 2, 28)
    assert qs.next_reset(b, now) == T(2027, 3, 28)


def test_monthly_anchor_crosses_year(qs: QuotaService) -> None:
    b = Budget(type="usd", limit_amount=10, window="monthly", reset_anchor=15)
    assert qs.period_start(b, T(2027, 1, 5)) == T(2026, 12, 15)
    assert qs.next_reset(b, T(2026, 12, 20)) == T(2027, 1, 15)


def test_monthly_anchor_null_counts_as_1(qs: QuotaService) -> None:
    b = Budget(type="usd", limit_amount=10, window="monthly", reset_anchor=None)
    now = T(2026, 10, 30, 12)
    assert qs.period_start(b, now) == T(2026, 10, 1)
    assert qs.next_reset(b, now) == T(2026, 11, 1)


def test_daily(qs: QuotaService) -> None:
    b = Budget(type="usd", limit_amount=10, window="daily")
    now = T(2026, 12, 31, 23, 59, 59)
    assert qs.period_start(b, now) == T(2026, 12, 31)
    assert qs.next_reset(b, now) == T(2027, 1, 1)


def test_weekly_starts_monday_utc(qs: QuotaService) -> None:
    b = Budget(type="usd", limit_amount=10, window="weekly")
    now = T(2026, 10, 30, 12)  # 週五
    start = qs.period_start(b, now)
    assert start == T(2026, 10, 26)
    assert start.weekday() == 0
    assert qs.next_reset(b, now) == T(2026, 11, 2)


def test_weekly_on_monday_midnight(qs: QuotaService) -> None:
    b = Budget(type="usd", limit_amount=10, window="weekly")
    assert qs.period_start(b, T(2026, 10, 26)) == T(2026, 10, 26)
    assert qs.period_start(b, T(2026, 10, 25, 23, 59, 59)) == T(2026, 10, 19)


def test_non_utc_now_is_converted(qs: QuotaService) -> None:
    b = Budget(type="usd", limit_amount=10, window="daily")
    taipei = timezone(timedelta(hours=8))
    now = datetime(2026, 10, 31, 3, 0, tzinfo=taipei)  # = 2026-10-30 19:00 UTC
    assert qs.period_start(b, now) == T(2026, 10, 30)


def test_window_none(qs: QuotaService) -> None:
    b = Budget(type="none", limit_amount=None, window="none")
    now = T(2026, 10, 30, 12)
    assert qs.period_start(b, now) is None
    assert qs.next_reset(b, now) == now + timedelta(hours=1)


def test_naive_now_rejected(qs: QuotaService) -> None:
    b = Budget(type="usd", limit_amount=10, window="daily")
    with pytest.raises(ValueError):
        qs.period_start(b, datetime(2026, 10, 30))


# ---------------------------------------------------------------- L1:無上限 / 上限 0(決策 6)


def test_limit_null_is_unlimited(db, qs: QuotaService) -> None:
    dep = _save(db, Budget(type="usd", limit_amount=None, window="monthly", reset_anchor=1))
    now = T(2026, 10, 30, 12)
    _spend(db, "a1", T(2026, 10, 5), 100.0)
    assert qs.remaining(dep, now) == math.inf
    assert qs.ratio(dep, now) == 1
    assert not qs.is_cooling(dep, now)


def test_no_budget_row_is_unlimited(db, qs: QuotaService) -> None:
    dep = _save(db, None)
    now = T(2026, 10, 30, 12)
    _spend(db, "a1", T(2026, 10, 5), 100.0)
    assert qs.remaining(dep, now) == math.inf
    assert qs.ratio(dep, now) == 1
    assert not qs.is_cooling(dep, now)


def test_type_none_ratio_is_1(db, qs: QuotaService) -> None:
    dep = _save(db, Budget(type="none", limit_amount=None, window="none"))
    now = T(2026, 10, 30, 12)
    assert qs.remaining(dep, now) == math.inf
    assert qs.ratio(dep, now) == 1


def test_limit_zero(db, qs: QuotaService) -> None:
    dep = _save(db, Budget(type="usd", limit_amount=0, window="monthly", reset_anchor=1))
    now = T(2026, 10, 30, 12)
    assert qs.remaining(dep, now) <= 0
    assert qs.ratio(dep, now) == 0


def test_ratio_partial_and_clamped(db, qs: QuotaService) -> None:
    dep = _save(db, Budget(type="usd", limit_amount=10, window="monthly", reset_anchor=1))
    now = T(2026, 10, 30, 12)
    _spend(db, "a1", T(2026, 10, 2), 2.5)
    assert qs.remaining(dep, now) == pytest.approx(7.5)
    assert qs.ratio(dep, now) == pytest.approx(0.75)
    _spend(db, "a1", T(2026, 10, 3), 20.0)
    assert qs.remaining(dep, now) == pytest.approx(-12.5)
    assert qs.ratio(dep, now) == 0


def test_now_defaults_to_clock(db, qs: QuotaService, clock: FakeClock) -> None:
    dep = _save(db, Budget(type="usd", limit_amount=10, window="daily"))
    _spend(db, "a1", T(2026, 10, 30, 1), 4.0)
    assert qs.remaining(dep) == pytest.approx(6.0)
    clock.set(T(2026, 10, 31, 0, 0, 1))
    assert qs.remaining(dep) == pytest.approx(10.0)


# ---------------------------------------------------------------- L1:mark / 冷卻(4.5.2、5.3.2、4.2.6)


def test_mark_quota_exhausted_sets_next_reset(db, qs: QuotaService) -> None:
    dep = _save(db, Budget(type="usd", limit_amount=10, window="monthly", reset_anchor=1))
    now = T(2026, 10, 30, 12)
    assert qs.mark(dep, "quota_exhausted", now) == T(2026, 11, 1)
    assert qs.is_cooling(dep, now)
    # 5.3.2:本地仍有剩餘也冷卻
    assert qs.remaining(dep, now) == pytest.approx(10.0)


def test_mark_rate_limited_sets_now_plus_60s(db, qs: QuotaService) -> None:
    dep = _save(db, Budget(type="usd", limit_amount=10, window="monthly", reset_anchor=1))
    now = T(2026, 10, 30, 12)
    assert qs.mark(dep, "rate_limited", now) == now + timedelta(seconds=60)


def test_mark_quota_exhausted_window_none_is_now_plus_1h(db, qs: QuotaService) -> None:
    dep = _save(db, Budget(type="none", limit_amount=None, window="none"))
    now = T(2026, 10, 30, 12)
    assert qs.mark(dep, "quota_exhausted", now) == now + timedelta(hours=1)


@pytest.mark.parametrize("status", ["ok", "error", "skipped_precheck"])
def test_mark_other_status_leaves_unchanged(db, qs: QuotaService, status: str) -> None:
    dep = _save(db, Budget(type="usd", limit_amount=10, window="daily"))
    now = T(2026, 10, 30, 12)
    assert qs.mark(dep, status, now) is None
    until = qs.mark(dep, "rate_limited", now)
    assert qs.mark(dep, status, now) == until
    assert repo.get_budget(db, "a1").exhausted_until == until


def test_cooling_auto_recovers_after_expiry(db, qs: QuotaService, clock: FakeClock) -> None:
    dep = _save(db, Budget(type="usd", limit_amount=10, window="daily"))
    now = T(2026, 10, 30, 12)
    until = qs.mark(dep, "rate_limited", now)
    assert qs.is_cooling(dep, until - timedelta(seconds=1))
    assert not qs.is_cooling(dep, until)
    assert not qs.is_cooling(dep, until + timedelta(seconds=1))
    clock.set(until + timedelta(seconds=1))
    assert not qs.is_cooling(dep)


def test_is_cooling_reads_store_not_stale_dep(db, qs: QuotaService) -> None:
    dep = _save(db, Budget(type="usd", limit_amount=10, window="daily"))
    now = T(2026, 10, 30, 12)
    qs.mark(dep, "quota_exhausted", now)
    assert dep.budget is not None and dep.budget.exhausted_until is None
    assert qs.is_cooling(dep, now)


# ---------------------------------------------------------------- L2:帳本與 budgets 列


def test_remaining_sums_only_current_period(db, qs: QuotaService) -> None:
    dep = _save(db, Budget(type="usd", limit_amount=10, window="monthly", reset_anchor=1))
    _save(db, None, dep_id="other")
    now = T(2026, 10, 30, 12)
    # 上期
    _spend(db, "a1", T(2026, 9, 30, 23, 59, 59), 5.0)
    _spend(db, "a1", T(2026, 9, 1), 3.0)
    # 本期
    _spend(db, "a1", T(2026, 10, 1, 0, 0, 0), 1.0)
    _spend(db, "a1", T(2026, 10, 15), 2.0)
    _spend(db, "a1", T(2026, 10, 20), None)            # 未計價
    _spend(db, "a1", T(2026, 10, 21), 0.5, "error")    # 有花費的失敗也算
    # 其他成員
    _spend(db, "other", T(2026, 10, 15), 9.0)
    assert qs.remaining(dep, now) == pytest.approx(10 - 3.5)


def test_remaining_window_none_sums_all(db, qs: QuotaService) -> None:
    dep = _save(db, Budget(type="credit_pool", limit_amount=10, window="none"))
    now = T(2026, 10, 30, 12)
    _spend(db, "a1", T(2025, 1, 1), 4.0)
    _spend(db, "a1", T(2026, 10, 30), 1.0)
    assert qs.remaining(dep, now) == pytest.approx(5.0)
    assert qs.ratio(dep, now) == pytest.approx(0.5)


def test_mark_persists_and_matches_return(db, qs: QuotaService) -> None:
    dep = _save(db, Budget(type="usd", limit_amount=10, window="weekly"))
    now = T(2026, 10, 30, 12)
    until = qs.mark(dep, "quota_exhausted", now)
    assert until == T(2026, 11, 2)
    assert repo.get_budget(db, "a1").exhausted_until == until
    until2 = qs.mark(dep, "rate_limited", now)
    assert repo.get_budget(db, "a1").exhausted_until == until2


def test_clear_sets_null(db, qs: QuotaService) -> None:
    dep = _save(db, Budget(type="usd", limit_amount=10, window="daily"))
    now = T(2026, 10, 30, 12)
    qs.mark(dep, "quota_exhausted", now)
    qs.clear(dep)
    assert repo.get_budget(db, "a1").exhausted_until is None
    assert not qs.is_cooling(dep, now)


def test_connection_factory_injection(db, clock: FakeClock) -> None:
    calls: list[int] = []

    def factory() -> sqlite3.Connection:
        calls.append(1)
        return db

    qs = QuotaService(factory, clock)
    dep = _save(db, Budget(type="usd", limit_amount=10, window="daily"))
    _spend(db, "a1", T(2026, 10, 30, 1), 1.0)
    assert qs.remaining(dep) == pytest.approx(9.0)
    assert calls
    # 工廠回傳的連線不由 QuotaService 關閉
    db.execute("SELECT 1")


def test_mark_without_budget_row_still_cools_and_stays_unlimited(db, qs: QuotaService) -> None:
    # 4.5.2:沒有 budgets 列的成員收到 429 仍要冷卻;補的列為 type=none,仍視為無上限(決策 6)
    dep = _save(db, None)
    now = T(2026, 10, 30, 12, 0, 0)
    until = qs.mark(dep, "rate_limited", now)
    assert until == now + timedelta(seconds=60)
    assert qs.is_cooling(dep, now)
    assert qs.remaining(dep, now) == math.inf and qs.ratio(dep, now) == 1
    assert not qs.is_cooling(dep, now + timedelta(seconds=61))


def test_mark_other_status_without_budget_row_creates_nothing(db, qs: QuotaService) -> None:
    dep = _save(db, None)
    assert qs.mark(dep, "error", T(2026, 10, 30, 12)) is None
    assert repo.get_budget(db, dep.id) is None

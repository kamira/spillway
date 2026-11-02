"""CHG-20261101-03 T1 L1:`store.repo` 冷卻函式(cooldowns 表;決策 3、6)。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from spillway.domain import Budget, Deployment
from spillway.store import repo

T0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)


def _save(db, dep_id: str = "d1", budget: Budget | None = None) -> None:
    repo.save_deployment(db, Deployment(id=dep_id, model_name="m", kind="api", provider="openai",
                                        model="gpt", budget=budget))


def test_roundtrip_and_stored_format(db):
    _save(db)
    assert repo.get_cooldown(db, "d1") is None
    repo.mark_cooldown(db, "d1", T0 + timedelta(minutes=5), "rate_limited")
    cd = repo.get_cooldown(db, "d1")
    assert cd == repo.Cooldown(T0 + timedelta(minutes=5), "rate_limited")
    assert cd.until.tzinfo is not None
    row = db.execute("SELECT * FROM cooldowns WHERE deployment_id = 'd1'").fetchone()
    assert (row["exhausted_until"], row["reason"]) == ("2026-10-30T12:05:00Z", "rate_limited")


def test_mark_keeps_later_time_and_its_reason(db):
    _save(db)
    repo.mark_cooldown(db, "d1", T0 + timedelta(hours=1), "quota_exhausted")
    # 較早的期限不縮短,原因也維持較晚那筆的
    repo.mark_cooldown(db, "d1", T0 + timedelta(minutes=1), "rate_limited")
    assert repo.get_cooldown(db, "d1") == repo.Cooldown(T0 + timedelta(hours=1), "quota_exhausted")
    # 相同期限也不改
    repo.mark_cooldown(db, "d1", T0 + timedelta(hours=1), "rate_limited")
    assert repo.get_cooldown(db, "d1").reason == "quota_exhausted"
    # 較晚的期限:期限與原因一起更新
    repo.mark_cooldown(db, "d1", T0 + timedelta(hours=2), "rate_limited")
    assert repo.get_cooldown(db, "d1") == repo.Cooldown(T0 + timedelta(hours=2), "rate_limited")


def test_clear_then_mark_can_shorten(db):
    """縮短冷卻只能先手動清除(5.3.3)再設定。"""
    _save(db)
    repo.mark_cooldown(db, "d1", T0 + timedelta(hours=1), "quota_exhausted")
    repo.clear_cooldown(db, "d1")
    repo.mark_cooldown(db, "d1", T0 - timedelta(seconds=1), "rate_limited")
    assert repo.get_cooldown(db, "d1") == repo.Cooldown(T0 - timedelta(seconds=1), "rate_limited")


def test_clear_deletes_row(db):
    _save(db)
    repo.mark_cooldown(db, "d1", T0, "quota_exhausted")
    assert repo.clear_cooldown(db, "d1") is True
    assert repo.get_cooldown(db, "d1") is None
    assert repo.clear_cooldown(db, "d1") is False
    assert db.execute("SELECT COUNT(*) FROM cooldowns").fetchone()[0] == 0


def test_cooldown_independent_of_budget_row(db):
    b = Budget(type="usd", limit_amount=5.0, window="daily")
    _save(db, budget=b)
    repo.mark_cooldown(db, "d1", T0, "quota_exhausted")
    assert repo.get_budget(db, "d1") == b
    repo.save_budget(db, "d1", None)            # 刪除額度不影響冷卻
    assert repo.get_cooldown(db, "d1") == repo.Cooldown(T0, "quota_exhausted")
    _save(db, budget=None)                      # 整筆重存成員(budget=None)也不影響冷卻
    assert repo.get_cooldown(db, "d1") is not None


def test_list_cooldowns(db):
    _save(db, "a")
    _save(db, "b")
    repo.mark_cooldown(db, "a", T0, "rate_limited")
    assert repo.list_cooldowns(db) == {"a": repo.Cooldown(T0, "rate_limited")}


def test_constraints(db):
    _save(db)
    with pytest.raises(sqlite3.IntegrityError):
        repo.mark_cooldown(db, "d1", T0, "error")          # reason CHECK
    with pytest.raises(sqlite3.IntegrityError):
        repo.mark_cooldown(db, "ghost", T0, "rate_limited")  # 外鍵


def test_delete_deployment_cascades_cooldown(db):
    _save(db)
    repo.mark_cooldown(db, "d1", T0, "rate_limited")
    repo.delete_deployment(db, "d1")
    assert db.execute("SELECT COUNT(*) FROM cooldowns").fetchone()[0] == 0

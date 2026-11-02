"""CHG-20261101-02 T3 L1:由 api.admin、quota、estimate 搬進 store.repo 的唯讀查詢。

每個函式以 `db` fixture 建資料後,回傳值與搬移前的 SQL(本檔以字串保留作為對照)相同。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from spillway.adapters.base.types import Budget, Deployment
from spillway.clock import to_iso
from spillway.domain import STATUS_SKIPPED_PRECHECK
from spillway.store import repo

NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)


def _dep(dep_id: str, provider: str = "anthropic", kind: str = "api",
         budget: Budget | None = None) -> Deployment:
    return Deployment(id=dep_id, model_name=f"m-{dep_id}", kind=kind, provider=provider,
                      model="x", budget=budget)


def _ledger(db: sqlite3.Connection, ts: datetime, *, dep: str | None = "a",
            route: str | None = "r1", status: str = "ok", cost: float | None = 1.0,
            tokens_out: int | None = 100) -> None:
    repo.insert_ledger(db, repo.LedgerRow(ts=ts, status=status, route_name=route,
                                          deployment_id=dep, cost_usd=cost,
                                          tokens_out=tokens_out))


# ---------------------------------------------------------------- deployments


def test_provider_member_ids(db):
    for i, prov in (("b", "anthropic"), ("a", "anthropic"), ("c", "openai"), ("d", "anthropic")):
        repo.save_deployment(db, _dep(i, provider=prov))
    old = [r[0] for r in db.execute(
        "SELECT id FROM deployments WHERE provider = ? AND id != ? ORDER BY id",
        ("anthropic", "b")).fetchall()]
    assert repo.provider_member_ids(db, "anthropic", exclude_id="b") == old == ["a", "d"]
    assert repo.provider_member_ids(db, "openai", exclude_id="c") == []
    assert repo.provider_member_ids(db, "none", exclude_id="x") == []


def test_deployment_exists(db):
    repo.save_deployment(db, _dep("a"))
    assert repo.deployment_exists(db, "a") is True
    assert repo.deployment_exists(db, "zz") is False


def test_list_kinds_providers(db):
    assert repo.list_kinds_providers(db) == []
    repo.save_deployment(db, _dep("a", provider="anthropic", kind="api"))
    repo.save_deployment(db, _dep("b", provider="claude", kind="cli"))
    old = [(r[0], r[1]) for r in db.execute("SELECT kind, provider FROM deployments")]
    got = repo.list_kinds_providers(db)
    assert got == old
    assert sorted(got) == [("api", "anthropic"), ("cli", "claude")]


# ---------------------------------------------------------------- ledger 計數


def _old_count(db, route=None, deployment=None, status=None, since=None, until=None) -> int:
    where: list[str] = []
    args: list[Any] = []
    for col, val in (("route_name", route), ("deployment_id", deployment), ("status", status)):
        if val is not None:
            where.append(f"{col} = ?")
            args.append(val)
    if since is not None:
        where.append("ts >= ?")
        args.append(to_iso(since))
    if until is not None:
        where.append("ts < ?")
        args.append(to_iso(until))
    sql = "SELECT COUNT(*) FROM usage_ledger" + (" WHERE " + " AND ".join(where) if where else "")
    return int(db.execute(sql, args).fetchone()[0])


@pytest.fixture
def ledger_db(db):
    for i in range(12):
        _ledger(db, NOW - timedelta(hours=i), dep="a" if i % 2 else "b",
                route=None if i % 3 == 0 else "r1",
                status=["ok", "error", STATUS_SKIPPED_PRECHECK][i % 3])
    return db


@pytest.mark.parametrize("kw", [
    {},
    {"route_name": "r1"},
    {"deployment_id": "a"},
    {"status": "error"},
    {"since": NOW - timedelta(hours=5)},
    {"until": NOW - timedelta(hours=5)},
    {"route_name": "r1", "deployment_id": "a", "status": "error",
     "since": NOW - timedelta(hours=10), "until": NOW - timedelta(hours=1)},
    {"deployment_id": "nobody"},
], ids=["empty", "route", "deployment", "status", "since", "until", "all", "none-match"])
def test_count_ledger_matches_old_sql(ledger_db, kw):
    old = _old_count(ledger_db, route=kw.get("route_name"), deployment=kw.get("deployment_id"),
                     status=kw.get("status"), since=kw.get("since"), until=kw.get("until"))
    assert repo.count_ledger(ledger_db, **kw) == old
    # 計數與清單同一組條件
    assert repo.count_ledger(ledger_db, **kw) == len(repo.list_ledger(ledger_db, **kw))


def test_count_ledger_empty_table(db):
    assert repo.count_ledger(db) == 0


# ---------------------------------------------------------------- 本期花費(quota)


def _old_spend(db, dep_id, start, end) -> float:
    sql = "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_ledger WHERE deployment_id = ?"
    args: list[object] = [dep_id]
    if start is not None:
        sql += " AND ts >= ? AND ts < ?"
        args += [to_iso(start), to_iso(end)]
    return float(db.execute(sql, args).fetchone()[0])


def test_ledger_spend_matches_old_sql(db):
    _ledger(db, NOW - timedelta(days=40), cost=10.0)
    _ledger(db, NOW - timedelta(days=2), cost=1.5)
    _ledger(db, NOW - timedelta(hours=1), cost=None)          # NULL 不計
    _ledger(db, NOW - timedelta(hours=1), cost=0.25, status="error")   # 不分 status
    _ledger(db, NOW - timedelta(hours=1), dep="b", cost=99.0)
    start, end = NOW - timedelta(days=7), NOW + timedelta(days=1)
    assert repo.ledger_spend(db, "a", start, end) == _old_spend(db, "a", start, end) == 1.75
    assert repo.ledger_spend(db, "a") == _old_spend(db, "a", None, None) == 11.75
    assert repo.ledger_spend(db, "zz") == 0.0
    assert isinstance(repo.ledger_spend(db, "zz"), float)


# ---------------------------------------------------------------- tokens_out 歷史(estimate)


def _old_tokens_out(db, dep_id, route_name, limit) -> list[int]:
    sql = (
        "SELECT tokens_out FROM usage_ledger "
        "WHERE deployment_id = ? AND status = 'ok' AND tokens_out IS NOT NULL AND "
        + ("route_name IS NULL" if route_name is None else "route_name = ?")
        + " ORDER BY ts DESC, id DESC LIMIT ?"
    )
    args: list[Any] = [dep_id]
    if route_name is not None:
        args.append(route_name)
    args.append(limit)
    return [int(r[0]) for r in db.execute(sql, args)]


def test_recent_tokens_out_matches_old_sql(db):
    for i in range(8):
        _ledger(db, NOW - timedelta(minutes=i), tokens_out=100 + i, route="r1")
        _ledger(db, NOW - timedelta(minutes=i), tokens_out=200 + i, route=None)
    _ledger(db, NOW, tokens_out=None, route="r1")                 # NULL 不佔名額
    _ledger(db, NOW, tokens_out=999, route="r1", status="error")  # 只取 ok
    _ledger(db, NOW, tokens_out=777, route="r1", dep="b")
    _ledger(db, NOW, tokens_out=555, route="r1")                  # 同 ts,id 較大者在前
    for route in ("r1", None, "nope"):
        for limit in (3, 50):
            assert repo.recent_tokens_out(db, "a", route, limit) == _old_tokens_out(db, "a", route, limit)
    assert repo.recent_tokens_out(db, "a", "r1", 3) == [555, 100, 101]
    assert repo.recent_tokens_out(db, "a", None, 2) == [200, 201]


# ---------------------------------------------------------------- dashboard 彙總


def test_dashboard_members_matches_old_join(db):
    repo.save_deployment(db, _dep("b", budget=Budget(type="usd", limit_amount=5.0, window="daily")))
    repo.save_deployment(db, _dep("a"))
    repo.save_deployment(db, _dep("c"))
    # schema v2(CHG-20261101-03 決策 4):v1 留下的 type=none 暫行列保留;冷卻改存 cooldowns
    repo.save_budget(db, "c", Budget(type="none", limit_amount=None, window="none"))
    repo.mark_cooldown(db, "c", NOW + timedelta(hours=1), "rate_limited")
    old = db.execute(
        "SELECT d.id, d.model_name, d.kind, d.provider, d.enabled, b.type AS b_type, "
        "b.limit_amount, b.window, b.reset_anchor, "
        "b.deployment_id IS NOT NULL AS has_budget, "
        "c.exhausted_until AS cooling_until, c.reason AS cooling_reason "
        "FROM deployments d LEFT JOIN budgets b ON b.deployment_id = d.id "
        "LEFT JOIN cooldowns c ON c.deployment_id = d.id ORDER BY d.id"
    ).fetchall()
    got = repo.dashboard_members(db)
    assert [dict(r) for r in got] == [dict(r) for r in old]
    by_id = {r["id"]: r for r in got}
    assert [r["id"] for r in got] == ["a", "b", "c"]
    assert by_id["a"]["has_budget"] == 0 and by_id["a"]["b_type"] is None
    assert by_id["c"]["has_budget"] == 1 and by_id["c"]["b_type"] == "none"
    assert by_id["c"]["window"] == "none" and by_id["c"]["limit_amount"] is None
    assert by_id["a"]["cooling_until"] is None and by_id["a"]["cooling_reason"] is None
    assert by_id["c"]["cooling_until"] == "2026-10-30T13:00:00Z"
    assert by_id["c"]["cooling_reason"] == "rate_limited"


def _old_spend_by_dep(db, start, end) -> dict[str, float]:
    sql = "SELECT deployment_id, COALESCE(SUM(cost_usd), 0) FROM usage_ledger"
    args: list[Any] = []
    if start is not None:
        sql += " WHERE ts >= ? AND ts < ?"
        args = [to_iso(start), to_iso(end)]
    sql += " GROUP BY deployment_id"
    return {d: float(s) for d, s in db.execute(sql, args)}


def test_spend_by_deployment_matches_old_sql(db):
    _ledger(db, NOW - timedelta(days=40), dep="a", cost=10.0)
    _ledger(db, NOW - timedelta(days=2), dep="a", cost=1.5)
    _ledger(db, NOW - timedelta(hours=1), dep="b", cost=None)
    _ledger(db, NOW - timedelta(hours=1), dep="c", cost=2.0, status=STATUS_SKIPPED_PRECHECK)
    _ledger(db, NOW - timedelta(hours=1), dep=None, cost=3.0)      # 孤兒列也在結果中(呼叫端不取用)
    # 日曆月(type=none 冷卻列與沒有 budgets 列同口徑)
    month_start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    month_end = datetime(2026, 11, 1, tzinfo=timezone.utc)
    for start, end in ((month_start, month_end), (None, None),
                       (NOW - timedelta(days=1), NOW + timedelta(days=1))):
        assert repo.spend_by_deployment(db, start, end) == _old_spend_by_dep(db, start, end)
    assert repo.spend_by_deployment(db, month_start, month_end) == {
        "a": 1.5, "b": 0.0, "c": 2.0, None: 3.0}
    assert repo.spend_by_deployment(db, None, None)["a"] == 11.5


def test_spend_by_deployment_is_one_grouped_query(db):
    for i in range(20):
        _ledger(db, NOW - timedelta(hours=i), dep=f"d{i}")
    seen: list[str] = []
    db.set_trace_callback(seen.append)
    try:
        repo.spend_by_deployment(db, NOW - timedelta(days=1), NOW + timedelta(days=1))
    finally:
        db.set_trace_callback(None)
    assert len(seen) == 1 and "GROUP BY deployment_id" in seen[0]


def _old_recent(db, now) -> dict[str, tuple[int, float]]:
    return {
        d: (int(n24), float(s7))
        for d, n24, s7 in db.execute(
            "SELECT deployment_id, "
            "SUM(CASE WHEN ts >= ? AND status != 'skipped_precheck' THEN 1 ELSE 0 END), "
            "COALESCE(SUM(cost_usd), 0) "
            "FROM usage_ledger WHERE ts >= ? GROUP BY deployment_id",
            (to_iso(now - timedelta(hours=24)), to_iso(now - timedelta(days=7))),
        )
    }


def test_recent_activity_matches_old_sql(db):
    _ledger(db, NOW - timedelta(hours=1), dep="a", cost=1.0)
    _ledger(db, NOW - timedelta(hours=2), dep="a", cost=None, status="error")
    _ledger(db, NOW - timedelta(hours=3), dep="a", cost=0.5, status=STATUS_SKIPPED_PRECHECK)
    _ledger(db, NOW - timedelta(days=3), dep="a", cost=2.0)
    _ledger(db, NOW - timedelta(days=3), dep="b", cost=4.0)
    _ledger(db, NOW - timedelta(days=9), dep="c", cost=8.0)      # 超過 7 天,不在結果
    got = repo.recent_activity(db, since_24h=NOW - timedelta(hours=24),
                               since_7d=NOW - timedelta(days=7))
    assert got == _old_recent(db, NOW)
    assert got == {"a": (2, 3.5), "b": (0, 4.0)}
    assert all(isinstance(n, int) and isinstance(s, float) for n, s in got.values())

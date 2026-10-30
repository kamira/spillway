"""`GET /api/dashboard`(spec 9.2、5.1、5.4.6;決策 6)(CHG-20261030-02 T14)。"""
from __future__ import annotations

import math
from datetime import timedelta

import pytest

from spillway.quota import QuotaService
from spillway.store import repo

from .helpers import api_member, local_member

pytestmark = pytest.mark.anyio


def spend(db, clock, dep, cost, *, hours_ago=0.0, days_ago=0, status="ok"):
    repo.insert_ledger(db, repo.LedgerRow(
        ts=clock.now() - timedelta(hours=hours_ago, days=days_ago), status=status,
        deployment_id=dep, route_name="rt", cost_usd=cost))


async def test_dashboard_matches_quota_service(client, db, clock):
    # clock = 2026-10-30T12:00Z
    await client.post("/api/deployments", json=api_member(
        "a", budget={"type": "usd", "limit_amount": 10, "window": "monthly", "reset_anchor": 25}))
    await client.post("/api/deployments", json=api_member(
        "d", budget={"type": "usd", "limit_amount": 2, "window": "daily"}))
    await client.post("/api/deployments", json=api_member("u"))                     # 無 budgets 列
    await client.post("/api/deployments", json=api_member(
        "n", budget={"type": "usd", "limit_amount": None}))                         # 上限 NULL
    await client.post("/api/deployments", json=local_member("q"))

    spend(db, clock, "a", 1.5, days_ago=1)            # 本期(10-25 起)
    spend(db, clock, "a", 0.5, hours_ago=2)
    spend(db, clock, "a", 4.0, days_ago=6)            # 10-24:上一期,不計
    spend(db, clock, "a", None, hours_ago=1, status="error")
    spend(db, clock, "d", 0.75, hours_ago=3)
    spend(db, clock, "d", 9.0, days_ago=2)            # 前天,不計
    spend(db, clock, "u", 0.25, hours_ago=30)
    spend(db, clock, "ghost", 100.0, hours_ago=1)     # 孤兒列(成員已刪)
    spend(db, clock, "a", None, hours_ago=1, status="skipped_precheck")
    repo.set_exhausted_until(db, "d", clock.now() + timedelta(minutes=5))

    r = await client.get("/api/dashboard")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["generated_at"] == "2026-10-30T12:00:00Z"
    rows = {d["id"]: d for d in out["deployments"]}
    assert list(rows) == ["a", "d", "n", "q", "u"]

    quota = QuotaService(db, clock)
    for dep_id, row in rows.items():
        dep = repo.load_deployment(db, dep_id)
        expected = quota.remaining(dep)
        if math.isinf(expected):
            assert row["remaining"] is None and row["unlimited"] is True
        else:
            assert row["unlimited"] is False
            assert row["remaining"] == pytest.approx(expected)
        assert row["ratio"] == pytest.approx(quota.ratio(dep))

    a = rows["a"]
    assert a["kind"] == "api" and a["provider"] == "openai" and a["enabled"] is True
    assert a["limit_amount"] == 10 and a["window"] == "monthly"
    assert a["period_spend"] == pytest.approx(2.0) and a["remaining"] == pytest.approx(8.0)
    assert a["period_start"] == "2026-10-25T00:00:00Z" and a["next_reset"] == "2026-11-25T00:00:00Z"
    assert a["requests_24h"] == 3                   # ok、error、ok(skipped_precheck 不算);6 天前不算
    assert a["cooling_until"] is None and a["cooling"] is False
    assert rows["d"]["period_spend"] == pytest.approx(0.75)
    assert rows["d"]["cooling_until"] == "2026-10-30T12:05:00Z" and rows["d"]["cooling"] is True
    assert rows["u"]["requests_24h"] == 0 and rows["u"]["limit_amount"] is None
    assert rows["q"]["kind"] == "local" and rows["q"]["unlimited"] is True
    # 5.4.6:剩餘 ÷ 近 7 天日均實際花費;a 的近 7 天實際花費 = 1.5 + 0.5 + 4.0 = 6.0
    assert a["est_days_left"] == pytest.approx(8.0 / (6.0 / 7))
    assert rows["u"]["est_days_left"] is None       # 無上限
    assert rows["n"]["est_days_left"] is None


async def test_expired_cooldown_not_reported(client, db, clock):
    await client.post("/api/deployments", json=api_member(
        "a", budget={"type": "usd", "limit_amount": 1}))
    repo.set_exhausted_until(db, "a", clock.now() - timedelta(seconds=1))
    (row,) = (await client.get("/api/dashboard")).json()["deployments"]
    assert row["cooling"] is False and row["cooling_until"] is None


async def test_zero_limit_and_window_none(client, db, clock):
    await client.post("/api/deployments", json=api_member(
        "z", budget={"type": "usd", "limit_amount": 0}))
    await client.post("/api/deployments", json=api_member(
        "w", budget={"type": "credit_pool", "limit_amount": 5, "window": "none"}))
    spend(db, clock, "w", 1.0, days_ago=200)           # window none:累計
    rows = {d["id"]: d for d in (await client.get("/api/dashboard")).json()["deployments"]}
    assert rows["z"]["remaining"] == 0 and rows["z"]["ratio"] == 0
    assert rows["w"]["period_spend"] == pytest.approx(1.0)
    assert rows["w"]["remaining"] == pytest.approx(4.0)
    assert rows["w"]["period_start"] is None
    assert rows["z"]["est_days_left"] is None          # 近 7 天沒有花費 → 「—」


async def test_dashboard_query_count_is_constant(client, db, clock, make_app, make_client,
                                                 db_path):
    """NFR-2:不逐成員查詢。以 sqlite trace 計數,100 與 5 個成員的查詢數相同。"""
    from spillway.store import db as store_db

    for i in range(5):
        await client.post("/api/deployments", json=api_member(f"m{i:03d}", budget={
            "type": "usd", "limit_amount": 5}))

    def count_queries() -> int:
        seen: list[str] = []
        app = make_app()

        def factory():
            c = store_db.connect(db_path)
            c.set_trace_callback(seen.append)
            return c

        app.state.conn_factory = factory
        return app, seen

    app, seen = count_queries()
    async with make_client(app) as c:
        assert (await c.get("/api/dashboard")).status_code == 200
    few = len(seen)
    for i in range(5, 100):
        await client.post("/api/deployments", json=api_member(f"m{i:03d}", budget={
            "type": "usd", "limit_amount": 5, "window": ["daily", "weekly", "monthly"][i % 3]}))
    app, seen = count_queries()
    async with make_client(app) as c:
        r = await c.get("/api/dashboard")
    assert len(r.json()["deployments"]) == 100
    assert len(seen) <= few + 3        # 期間種類變多時最多多幾個彙總查詢,不隨成員數成長

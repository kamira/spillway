"""末端審查 U10 (a):沒有 budgets 列的成員被設冷卻後,儀表板口徑不變。

CHG-20261101-03(schema v2,決策 6)起冷卻存在 `cooldowns`,`mark` 不再建立 type=none 暫行列:
- `/api/dashboard`:比照「沒有 budgets 列」——本期花費以日曆月計(決策 48),`budget_type`、
  `limit_amount`、`window` 為 null;冷卻欄位照實回傳。v1 留下的暫行列(migration 保留)口徑相同。
- `GET /api/deployments/{id}`:`budget` 為 null,冷卻在頂層 `cooling_until`、`cooling_reason`。
- `PUT /api/deployments/{id}` 的 `budget` 為 null:只刪除 budgets 列;冷卻不受影響。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from spillway.domain import Budget
from spillway.store import repo
from tests.helpers import api_member

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("fake_key_env")]

MSGS = [{"role": "user", "content": "hi"}]


def _setup(h, base_url, *, a_budget=None):
    h.add_deployment(api_member("a", base_url, model="gpt-4o-mini", model_name="mA", tags=("chat",),
                                budget=a_budget))
    h.add_deployment(api_member("b", base_url, model="gpt-4o", model_name="mB", tags=("chat",)))
    h.add_route(repo.Route(name="r", target={"tags": {"all": ["chat"]}}),
                [repo.RouteTarget("a", 1, 0), repo.RouteTarget("b", 2, 0)])


def _spend(h, dep_id, ts, cost):
    with h.db() as conn:
        repo.insert_ledger(conn, repo.LedgerRow(ts=ts, status="ok", deployment_id=dep_id,
                                                route_name="r", cost_usd=cost))


def _dash(out):
    return {d["id"]: d for d in out["deployments"]}


async def test_probe1_member_without_budget_429_dashboard_stays_calendar_month(spillway, fake_api):
    # clock = 2026-10-30T12:00Z
    _setup(spillway, fake_api.base_url)
    _spend(spillway, "a", spillway.clock.now() - timedelta(days=40), 5.0)   # 上個月以前,不計
    _spend(spillway, "a", spillway.clock.now() - timedelta(days=2), 0.25)   # 本月
    before = _dash((await spillway.client.get("/api/dashboard")).json())["a"]
    assert (before["budget_type"], before["window"], before["period_start"]) == \
        (None, None, "2026-10-01T00:00:00Z")

    fake_api.set_mode("gpt-4o-mini", "http_429")
    r = await spillway.client.post("/v1/chat/completions", json={"model": "r", "messages": MSGS})
    assert r.status_code == 200 and r.headers["X-Spillway-Served-By"] == "b", r.text
    with spillway.db() as conn:
        assert repo.get_budget(conn, "a") is None          # 決策 6:mark 不再建 budgets 列
        assert repo.get_cooldown(conn, "a") is not None

    after = _dash((await spillway.client.get("/api/dashboard")).json())["a"]
    for k in ("budget_type", "limit_amount", "window", "period_start", "next_reset",
              "period_spend", "remaining", "unlimited", "ratio"):
        assert after[k] == before[k], k
    assert after["period_spend"] == pytest.approx(0.25)
    assert after["next_reset"] == "2026-11-01T00:00:00Z"
    assert after["cooling"] is True and after["cooling_until"] == "2026-10-30T12:01:00Z"

    # GET deployment:budget 為 null,冷卻在頂層
    got = (await spillway.client.get("/api/deployments/a")).json()
    assert got["budget"] is None
    assert got["cooling_until"] == "2026-10-30T12:01:00Z" and got["cooling_reason"] == "rate_limited"


async def test_v1_placeholder_row_kept_and_dashboard_treats_as_no_budget(spillway, fake_api):
    """v1 留下的暫行列(migration 保留,決策 4):儀表板仍比照沒有 budgets 列(`spillway.quota.is_cooling_only`)。"""
    _setup(spillway, fake_api.base_url, a_budget=Budget("none", None, "none"))
    _spend(spillway, "a", spillway.clock.now() - timedelta(days=2), 0.25)
    row = _dash((await spillway.client.get("/api/dashboard")).json())["a"]
    assert (row["budget_type"], row["window"], row["period_start"]) == \
        (None, None, "2026-10-01T00:00:00Z")
    assert row["unlimited"] is True and row["cooling"] is False


async def test_explicit_budget_rows_unchanged(spillway, fake_api):
    _setup(spillway, fake_api.base_url, a_budget=Budget("credit_pool", 10.0, "none"))
    _spend(spillway, "a", spillway.clock.now() - timedelta(days=40), 5.0)
    row = _dash((await spillway.client.get("/api/dashboard")).json())["a"]
    assert (row["budget_type"], row["window"], row["period_start"]) == ("credit_pool", "none", None)
    assert row["period_spend"] == pytest.approx(5.0) and row["remaining"] == pytest.approx(5.0)


def _put_body(got):
    # cooling_until、cooling_reason 為唯讀回應欄位,不送回
    body = {k: v for k, v in got.items() if k not in ("id", "cooling_until", "cooling_reason")}
    body["budget"] = None
    return body


async def test_put_budget_null_keeps_active_cooldown(spillway, fake_api):
    _setup(spillway, fake_api.base_url, a_budget=Budget("usd", 10.0, "monthly", reset_anchor=1))
    fake_api.set_mode("gpt-4o-mini", "http_429")
    await spillway.client.post("/v1/chat/completions", json={"model": "r", "messages": MSGS})
    got = (await spillway.client.get("/api/deployments/a")).json()
    assert got["cooling_until"] == "2026-10-30T12:01:00Z"

    r = await spillway.client.put("/api/deployments/a", json=_put_body(got))
    assert r.status_code == 200, r.text
    assert r.json()["budget"] is None                       # 決策 6:budget: null 只刪除 budgets 列
    assert r.json()["cooling_until"] == "2026-10-30T12:01:00Z"
    with spillway.db() as conn:
        assert repo.get_budget(conn, "a") is None
    row = _dash((await spillway.client.get("/api/dashboard")).json())["a"]
    assert row["cooling"] is True and row["budget_type"] is None and row["unlimited"] is True
    # 冷卻仍生效:下一次請求不呼叫 a
    n0 = len(fake_api.requests)
    r2 = await spillway.client.post("/v1/chat/completions", json={"model": "r", "messages": MSGS})
    assert r2.headers["X-Spillway-Served-By"] == "b" and len(fake_api.requests) - n0 == 1


async def test_put_budget_null_without_active_cooldown_deletes_row(spillway, fake_api):
    _setup(spillway, fake_api.base_url, a_budget=Budget("usd", 10.0, "daily"))
    with spillway.db() as conn:
        repo.mark_cooldown(conn, "a", spillway.clock.now() - timedelta(seconds=1),
                           "rate_limited")   # 已到期
    got = (await spillway.client.get("/api/deployments/a")).json()
    r = await spillway.client.put("/api/deployments/a", json=_put_body(got))
    assert r.status_code == 200 and r.json()["budget"] is None
    with spillway.db() as conn:
        assert repo.get_budget(conn, "a") is None

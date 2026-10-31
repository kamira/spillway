"""末端審查 U10 (a):只為記錄冷卻而建的 budgets 列(type=none、window=none、limit_amount NULL)。

- `/api/dashboard`:比照「沒有 budgets 列」——本期花費以日曆月計(決策 48),`budget_type`、
  `limit_amount`、`window` 為 null;冷卻欄位照實回傳。
- `GET /api/deployments/{id}`:照實呈現該列(含 `exhausted_until`)。
- `PUT /api/deployments/{id}` 的 `budget` 為 null:冷卻未到期時保留(列改為只記冷卻),否則刪除。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from spillway.domain import Budget
from spillway.store import repo
from tests.integration.conftest import TEST_KEY, TEST_KEY_ENV, api_member

pytestmark = pytest.mark.anyio

MSGS = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY)


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
        b = repo.get_budget(conn, "a")
    assert (b.type, b.window, b.limit_amount) == ("none", "none", None)   # mark 建的冷卻列

    after = _dash((await spillway.client.get("/api/dashboard")).json())["a"]
    for k in ("budget_type", "limit_amount", "window", "period_start", "next_reset",
              "period_spend", "remaining", "unlimited", "ratio"):
        assert after[k] == before[k], k
    assert after["period_spend"] == pytest.approx(0.25)
    assert after["next_reset"] == "2026-11-01T00:00:00Z"
    assert after["cooling"] is True and after["cooling_until"] == "2026-10-30T12:01:00Z"

    # GET deployment 照實呈現該列
    got = (await spillway.client.get("/api/deployments/a")).json()
    assert got["budget"] == {"type": "none", "limit_amount": None, "window": "none",
                             "reset_anchor": None, "exhausted_until": "2026-10-30T12:01:00Z"}


async def test_explicit_budget_rows_unchanged(spillway, fake_api):
    _setup(spillway, fake_api.base_url, a_budget=Budget("credit_pool", 10.0, "none"))
    _spend(spillway, "a", spillway.clock.now() - timedelta(days=40), 5.0)
    row = _dash((await spillway.client.get("/api/dashboard")).json())["a"]
    assert (row["budget_type"], row["window"], row["period_start"]) == ("credit_pool", "none", None)
    assert row["period_spend"] == pytest.approx(5.0) and row["remaining"] == pytest.approx(5.0)


def _put_body(got):
    body = {k: v for k, v in got.items() if k != "id"}
    body["budget"] = None
    return body


async def test_put_budget_null_keeps_active_cooldown(spillway, fake_api):
    _setup(spillway, fake_api.base_url, a_budget=Budget("usd", 10.0, "monthly", reset_anchor=1))
    fake_api.set_mode("gpt-4o-mini", "http_429")
    await spillway.client.post("/v1/chat/completions", json={"model": "r", "messages": MSGS})
    got = (await spillway.client.get("/api/deployments/a")).json()
    assert got["budget"]["exhausted_until"] == "2026-10-30T12:01:00Z"

    r = await spillway.client.put("/api/deployments/a", json=_put_body(got))
    assert r.status_code == 200, r.text
    assert r.json()["budget"] == {"type": "none", "limit_amount": None, "window": "none",
                                  "reset_anchor": None, "exhausted_until": "2026-10-30T12:01:00Z"}
    row = _dash((await spillway.client.get("/api/dashboard")).json())["a"]
    assert row["cooling"] is True and row["budget_type"] is None and row["unlimited"] is True
    # 冷卻仍生效:下一次請求不呼叫 a
    n0 = len(fake_api.requests)
    r2 = await spillway.client.post("/v1/chat/completions", json={"model": "r", "messages": MSGS})
    assert r2.headers["X-Spillway-Served-By"] == "b" and len(fake_api.requests) - n0 == 1


async def test_put_budget_null_without_active_cooldown_deletes_row(spillway, fake_api):
    _setup(spillway, fake_api.base_url, a_budget=Budget("usd", 10.0, "daily"))
    with spillway.db() as conn:
        repo.set_exhausted_until(conn, "a", spillway.clock.now() - timedelta(seconds=1))   # 已到期
    got = (await spillway.client.get("/api/deployments/a")).json()
    r = await spillway.client.put("/api/deployments/a", json=_put_body(got))
    assert r.status_code == 200 and r.json()["budget"] is None
    with spillway.db() as conn:
        assert repo.get_budget(conn, "a") is None

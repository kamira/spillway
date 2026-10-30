"""m2_quota.feature 專用步驟(CHG-20261030-02 T15;4.2.5、4.2.6 的 api 版,AC-16、AC-17)。"""
from __future__ import annotations

from datetime import timedelta

from behave import step, use_step_matcher

from spillway.store import repo
from tests.features.support.gateway import DEFAULT_MESSAGES

use_step_matcher("re")

PRICE_TOLERANCE = 0.0005   # 美元;預估 P90 花費與目標值的允差


def _estimate_for(gw, model: str, dep_id: str) -> dict:
    out = gw.ok(gw.post("/api/estimate", json={"model": model, "messages": DEFAULT_MESSAGES}), 200)
    for c in out["candidates"]:
        if c["deployment"] == dep_id:
            return {**c, "reason": None}
    for x in out["excluded"]:
        if x["deployment"] == dep_id:
            return x
    raise AssertionError(f"/api/estimate 沒有成員 {dep_id}:{out}")


@step(r'成員 "([^"]+)" 剩餘額度為 ([0-9.]+) 美元且本次請求預估 P90 花費為 ([0-9.]+) 美元')
def step_remaining_and_estimate(context, dep_id, remaining, est_cost):
    """前置條件:
    1. 經管理 API 設定額度 usd、上限 = remaining、每月 1 日重置(本期無花費,剩餘 = 上限)。
    2. 讓本次請求的 P90 花費 = est_cost:在同 route、同成員寫入 5 筆 ok 帳本(cost 0,不影響剩餘),
       其 tokens_out 依 LiteLLM 價格表反推(5.4.3:≥ 5 筆時取帳本 P90)。
    3. 經 `/api/estimate` 確認預估花費與剩餘。
    """
    gw = context.gw
    remaining, est_cost = float(remaining), float(est_cost)
    gw.update_member(dep_id, budget={"type": "usd", "limit_amount": remaining, "window": "monthly",
                                     "reset_anchor": 1})
    route = next(iter(gw.routes))
    first = _estimate_for(gw, route, dep_id)["estimate"]
    from spillway.adapters.base import load_litellm

    cin, cout = load_litellm().cost_per_token(model=f"openai/{gw.model_of(dep_id)}",
                                              prompt_tokens=first["tokens_in"], completion_tokens=1)
    tokens_out = round((est_cost - cin) / cout)
    now = gw.clock.now()
    with gw.db() as conn:
        for i in range(5):
            repo.insert_ledger(conn, repo.LedgerRow(
                ts=now - timedelta(hours=1, minutes=i), status="ok", route_name=route,
                deployment_id=dep_id, slot=1, tokens_in=first["tokens_in"], tokens_out=tokens_out,
                cost_usd=0.0))
    after = _estimate_for(gw, route, dep_id)
    cost = after["estimate"]["cost_usd_p90"]
    assert abs(cost - est_cost) < PRICE_TOLERANCE, after
    row = gw.dashboard_row(dep_id)
    assert row["remaining"] == remaining and row["cooling"] is False, row
    route_body = gw.routes[route]
    if route_body.get("budget_precheck"):
        assert after.get("reason") == "skipped_precheck", after


@step(r'成員 "([^"]+)" 的額度為每月重置、重置日為 (\d+) 日')
def step_monthly_budget(context, dep_id, anchor):
    gw = context.gw
    gw.update_member(dep_id, budget={"type": "usd", "limit_amount": 100.0, "window": "monthly",
                                     "reset_anchor": int(anchor)})
    b = gw.deployment(dep_id)["budget"]
    assert b["window"] == "monthly" and b["reset_anchor"] == int(anchor), b
    assert gw.dashboard_row(dep_id)["ratio"] == 1

"""m2_estimate.feature 專用步驟(CHG-20261030-02 T15;AC-18、5.4.3)。

(CHG-20261107-03 T4)歷史帳本經 `POST /__test__/ledger` 種入(spec 13.4.3)。
"""
from __future__ import annotations

import math
import random
from datetime import timedelta

from behave import step, use_step_matcher

from tests.blackbox.control import iso

use_step_matcher("re")

HISTORY_SEED = 18


def _nearest_rank(values: list[int], p: int) -> int:
    """nearest-rank 百分位(本檔獨立實作,不引用 spillway.estimate):排序後取第 ceil(p/100 × n) 個。"""
    ordered = sorted(values)
    return ordered[max(1, math.ceil(p * len(ordered) / 100)) - 1]


@step(r'route "([^"]+)" 與成員 "([^"]+)" 已有 (\d+) 筆 ok 帳本紀錄')
def step_seed_history(context, route, dep_id, n):
    """前置條件:經 `POST /__test__/ledger` 種入 n 筆同 route、同成員的 ok 列(tokens_out 由固定 seed 的
    亂數產生),另夾雜不應計入的列:同成員的 error 列、他 route 的 ok 列(tokens_out 極大,誤算會明顯偏移)。"""
    gw = context.gw
    gw.ensure_started()
    rng = random.Random(HISTORY_SEED)
    now = gw.clock.now()
    rows = []
    for i in range(int(n)):
        ts = iso(now - timedelta(hours=2) + timedelta(seconds=i * 60))
        rows.append({"ts": ts, "status": "ok", "route_name": route, "deployment_id": dep_id, "slot": 1,
                     "tokens_in": 10, "tokens_out": 50 + int(rng.random() * 3000), "cost_usd": 0.0001})
        if i % 10 == 0:
            rows.append({"ts": ts, "status": "error", "route_name": route, "deployment_id": dep_id, "slot": 1,
                         "tokens_out": 99999, "error": "upstream error"})
            rows.append({"ts": ts, "status": "ok", "route_name": "other-route", "deployment_id": dep_id,
                         "tokens_out": 88888, "cost_usd": 0.0})
    ids = gw.control.ledger(rows)
    assert len(ids) == len(rows), ids


@step(r'成員 "([^"]+)" 的預估 P50 與 P90 等於由帳本最近 (\d+) 筆 ok 的 tokens_out 計算的值')
def step_estimate_matches_ledger(context, dep_id, n):
    gw = context.gw
    plan = gw.plans[-1]
    route = plan["route_name"]
    # 經 HTTP 帳本查詢取同 route、同成員、status ok 的最近 n 筆(新到舊)
    items = gw.ledger(deployment=dep_id, route=route, status="ok", page_size=int(n))["items"]
    assert len(items) == int(n), len(items)
    outs = [r["tokens_out"] for r in items]
    expected = (_nearest_rank(outs, 50), _nearest_rank(outs, 90))
    est = gw.plan_entry(plan, dep_id)["estimate"]
    print(f"AC-18:帳本計算 P50={expected[0]} P90={expected[1]};dry-run P50={est['out_p50']} P90={est['out_p90']}")
    assert (est["out_p50"], est["out_p90"]) == expected, (est, expected)

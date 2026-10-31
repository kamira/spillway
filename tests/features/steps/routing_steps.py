"""m2_routing.feature 專用步驟(CHG-20261030-02 T15;AC-3、AC-4、AC-5、4.5)。"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from behave import step, use_step_matcher

from spillway.quota import QuotaService

use_step_matcher("re")


@step(r'成員 "([^"]+)" 與 "([^"]+)" 在冷卻中')
def step_two_cooling(context, a, b):
    """前置條件:以 QuotaService 的同一寫入路徑設定冷卻(現在 + 1 小時),再經 HTTP 確認。"""
    gw = context.gw
    until = gw.clock.now() + timedelta(hours=1)
    from spillway.store import repo

    with gw.db() as conn:
        for dep_id in (a, b):
            repo.mark_exhausted_until(conn, dep_id, until)
        for dep_id in (a, b):
            assert QuotaService(conn, gw.clock).is_cooling(repo.load_deployment(conn, dep_id))
    for dep_id in (a, b):
        assert gw.dashboard_row(dep_id)["cooling"] is True


@step(r"假上游總共收到 (\d+) 次請求")
def step_fake_total(context, n):
    reqs = context.gw.fake.requests
    assert len(reqs) == int(n), [(r.method, r.path, r.model) for r in reqs]


@step(r'"([^"]+)" 被選為第一順位的比例介於 ([0-9.]+) 與 ([0-9.]+)')
def step_first_ratio(context, dep_id, lo, hi):
    plans = context.gw.plans
    assert plans, "沒有 dry-run 結果"
    firsts = Counter(p["first"] for p in plans)
    ratio = firsts[dep_id] / len(plans)
    context.gw.first_ratio = ratio
    print(f"AC-4:{dep_id} 第一順位 {firsts[dep_id]}/{len(plans)} = {ratio:.4f};分布 {dict(firsts)}")
    assert float(lo) <= ratio <= float(hi), f"{dep_id} 比例 {ratio:.4f},分布 {dict(firsts)}"


@step(r"該次帳本列的 slot 為 NULL")
def step_ledger_slot_null(context):
    gw = context.gw
    served = gw.last.headers["X-Spillway-Served-By"]
    row = gw.latest_ledger(deployment=served)
    assert row["id"] == gw.last_ledger_id, row
    assert row["slot"] is None and row["route_name"] is None, row
    with gw.db() as conn:
        assert conn.execute("SELECT slot FROM usage_ledger WHERE id = ?", (row["id"],)).fetchone()[0] is None


@step(r'錯誤內容依序列出 "([^"]+)"')
def step_error_attempts(context, expected):
    message = context.gw.last.json()["error"]["message"]
    # 決策 31 的格式:「沒有可用的候選成員;嘗試:a:error, c:error[;被篩:…]」
    tried_part = next(p for p in message.split(";") if p.startswith("嘗試:"))
    tried = [x.strip() for x in tried_part.removeprefix("嘗試:").split(",")]
    assert tried == [x.strip() for x in expected.split(",")], message

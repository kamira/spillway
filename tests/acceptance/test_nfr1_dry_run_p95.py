"""NFR-1:路由決策耗時 P95 < 20 ms(不含上游呼叫)。

量測方式依 spec 11:100 個成員、當月 10,000 列帳本,連續 1,000 次 dry-run。與 AC-7 為同一量測
(`tests/acceptance/perf.py` 的 `dry_run_measurement()`,同一 pytest 行程內共用結果)。
黑箱(CHG-20261107-03 決策 10 / AM-11):只經 HTTP 計時(含序列化與 TCP 往返,較嚴),門檻不變;轉換前的
行程內 `Router.dry_run()` 直接量測已移除(已知限制 3)。excluded 的 reason 分布與 local 健康狀態必須同轉換前。

task route 變體(CHG-20261102-01 T8;決策 5 ②;ACC-20261101-02 F4):既有量測的 route `nfr` 為 chat mode,
2 個 cli 成員在候選篩選第 2 條(`mode_chat_excludes_cli`)即被篩除,走不到冷卻與併發名額的檢查。變體用同一份
資料、同樣 target 與 `budget_precheck`,但 route 為 task mode,並讓 claude-cli 成員(m000)冷卻中
(`POST /__test__/cooldowns`)、codex-cli 成員(m001)併發名額已滿(`POST /__test__/cli-slots/acquire`
佔住唯一名額,不起任何子行程;spec 13.4.8),再量 1,000 次;名額以 `GET /__test__/cli-slots` 觀察。
"""
from __future__ import annotations

import time
from datetime import timedelta

from tests.acceptance.perf import (
    BASELINE_EXCLUDED,
    DRY_RUN_COUNT,
    DRY_RUN_P95_LIMIT_MS,
    DRY_RUN_WARMUP,
    MESSAGES,
    TRANSPORT,
    dry_run_measurement,
    open_serve,
    prepare,
    summary,
)
from tests.acceptance.results import record_result
from tests.acceptance.seed import TAG

TASK_ROUTE = "nfr-task"
TASK_META = {"mode": "task"}


def test_nfr1_routing_decision_p95_under_20ms(serve_factory, fake_api, test_key_env):
    m = dry_run_measurement(serve_factory, fake_api)
    assert m["http"]["n"] == DRY_RUN_COUNT
    assert m["candidates"] + m["excluded"] == 100
    assert m["excluded_reasons"] == BASELINE_EXCLUDED and m["local_health"] == ["healthy"], m
    assert m["http"]["p95_ms"] < DRY_RUN_P95_LIMIT_MS, m


def test_nfr1_task_route_with_cli_members_p95_under_20ms(serve_factory, fake_api, test_key_env):
    h = open_serve(serve_factory)
    ds = prepare(h, fake_api.base_url)
    claude_id, codex_id = ds.cli_ids
    r = h.request("POST", "/api/routes", json={
        "name": TASK_ROUTE, "target": {"tags": {"all": [], "any": [TAG], "none": []}}, "mode": "task",
        "tags": [], "record_context": None, "budget_precheck": True})
    assert r.status_code == 201, r.text
    h.control.cooldown(claude_id, h.control.now() + timedelta(hours=1), "quota_exhausted")
    assert h.control.acquire(codex_id, 1)["in_flight"] == 1
    try:
        body = {"messages": MESSAGES, "metadata": TASK_META}
        url = f"/api/routes/{TASK_ROUTE}/dry-run"
        plan: dict = {}
        with h.http() as c:
            for _ in range(DRY_RUN_WARMUP):
                r = c.post(url, json=body)
                assert r.status_code == 200, r.text
                plan = r.json()
            entries = [e for s in plan["slots"] for e in s["entries"]]
            excluded = {x["deployment"]: x["reason"] for x in plan["excluded"]}
            assert {e["deployment"] for e in entries} | set(excluded) == set(ds.member_ids)
            # 兩個 cli 成員走到冷卻與併發名額的檢查(不是 chat mode 的篩除)
            assert excluded.get(claude_id) == "cooling", excluded
            assert excluded.get(codex_id) == "concurrency_full", excluded
            assert all(e["health"] == "healthy" for e in entries if e["kind"] == "local"), entries

            http: list[float] = []
            for _ in range(DRY_RUN_COUNT):
                t0 = time.perf_counter()
                r = c.post(url, json=body)
                http.append((time.perf_counter() - t0) * 1000)
                assert r.status_code == 200
        slots = {s["deployment"]: s["in_flight"] for s in h.control.cli_slots()}
        assert slots[codex_id] == 1          # dry-run 不佔、不釋放名額
    finally:
        h.control.release(codex_id)
    m = {"http": summary(http), "transport": TRANSPORT, "route_mode": "task",
         "candidates": len(entries), "excluded": len(plan["excluded"]),
         "cli_excluded": {claude_id: excluded[claude_id], codex_id: excluded[codex_id]}}
    record_result("nfr1_task_route_dry_run", m)
    assert m["candidates"] + m["excluded"] == 100
    assert m["http"]["n"] == DRY_RUN_COUNT
    assert m["http"]["p95_ms"] < DRY_RUN_P95_LIMIT_MS, m

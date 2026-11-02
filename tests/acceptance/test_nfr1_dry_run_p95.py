"""NFR-1:路由決策耗時 P95 < 20 ms(不含上游呼叫)。

量測方式依 spec 11:100 個成員、當月 10,000 列帳本,連續 1,000 次 dry-run。與 AC-7 為同一量測
(`tests/acceptance/perf.py` 的 `dry_run_measurement()`,同一 pytest 行程內共用結果)。
經 HTTP 的量測(含序列化,較嚴)與直接呼叫 `Router.dry_run()` 的量測都必須 < 20 ms。

task route 變體(CHG-20261102-01 T8;決策 5 ②;ACC-20261101-02 F4):既有量測的 route `nfr` 為 chat mode,
2 個 cli 成員在候選篩選第 2 條(`mode_chat_excludes_cli`)即被篩除,走不到冷卻與併發名額的檢查。變體用同一份
資料、同樣 target 與 `budget_precheck`,但 route 為 task mode,並讓 claude-cli 成員(m000)冷卻中、codex-cli
成員(m001)併發名額已滿(`cli_runner.try_acquire(m001, 1)` 佔住唯一名額,不起任何子行程),再量 1,000 次。
"""
from __future__ import annotations

import time
from datetime import timedelta

import pytest

from spillway.domain import ChatRequest
from spillway.store import repo
from tests.acceptance.perf import (
    DRY_RUN_COUNT,
    DRY_RUN_P95_LIMIT_MS,
    DRY_RUN_WARMUP,
    MESSAGES,
    dry_run_measurement,
    prepare,
    summary,
)
from tests.acceptance.results import record_result
from tests.acceptance.seed import TAG

pytestmark = pytest.mark.anyio

TASK_ROUTE = "nfr-task"
TASK_META = {"mode": "task"}


async def test_nfr1_routing_decision_p95_under_20ms(open_spillway, fake_api, test_key_env):
    m = await dry_run_measurement(open_spillway, fake_api)
    assert m["http"]["n"] == m["router_direct"]["n"] == DRY_RUN_COUNT
    assert m["candidates"] + m["excluded"] == 100
    assert m["router_direct"]["p95_ms"] < DRY_RUN_P95_LIMIT_MS, m
    assert m["http"]["p95_ms"] < DRY_RUN_P95_LIMIT_MS, m


async def test_nfr1_task_route_with_cli_members_p95_under_20ms(open_spillway, fake_api, test_key_env):
    async with open_spillway() as h:
        ds = await prepare(h, fake_api.base_url)
        claude_id, codex_id = ds.cli_ids
        with h.db() as conn:
            repo.save_route(conn, repo.Route(
                name=TASK_ROUTE, target={"tags": {"all": [], "any": [TAG], "none": []}}, mode="task",
                tags=(), record_context=None, budget_precheck=True))
            repo.mark_cooldown(conn, claude_id, h.clock.now() + timedelta(hours=1), "quota_exhausted")
        runner = h.app.state.cli_runner
        assert runner.try_acquire(codex_id, 1)
        try:
            body = {"messages": MESSAGES, "metadata": TASK_META}
            url = f"/api/routes/{TASK_ROUTE}/dry-run"
            plan: dict = {}
            for _ in range(DRY_RUN_WARMUP):
                r = await h.client.post(url, json=body)
                assert r.status_code == 200, r.text
                plan = r.json()
            entries = [e for s in plan["slots"] for e in s["entries"]]
            excluded = {x["deployment"]: x["reason"] for x in plan["excluded"]}
            assert {e["deployment"] for e in entries} | set(excluded) == set(ds.member_ids)
            # 兩個 cli 成員走到冷卻與併發名額的檢查(不是 chat mode 的篩除)
            assert excluded.get(claude_id) == "cooling", excluded
            assert excluded.get(codex_id) == "concurrency_full", excluded

            http: list[float] = []
            for _ in range(DRY_RUN_COUNT):
                t0 = time.perf_counter()
                r = await h.client.post(url, json=body)
                http.append((time.perf_counter() - t0) * 1000)
                assert r.status_code == 200

            req = ChatRequest(model=f"route:{TASK_ROUTE}", messages=MESSAGES, metadata=dict(TASK_META))
            direct: list[float] = []
            for _ in range(DRY_RUN_COUNT):
                t0 = time.perf_counter()
                await h.app.state.router.dry_run(f"route:{TASK_ROUTE}", req)
                direct.append((time.perf_counter() - t0) * 1000)
            assert runner.in_flight(codex_id) == 1          # dry-run 不佔、不釋放名額
        finally:
            runner.release(codex_id)
    m = {"http": summary(http), "router_direct": summary(direct), "route_mode": "task",
         "candidates": len(entries), "excluded": len(plan["excluded"]),
         "cli_excluded": {claude_id: excluded[claude_id], codex_id: excluded[codex_id]}}
    record_result("nfr1_task_route_dry_run", m)
    assert m["candidates"] + m["excluded"] == 100
    assert m["http"]["n"] == m["router_direct"]["n"] == DRY_RUN_COUNT
    assert m["router_direct"]["p95_ms"] < DRY_RUN_P95_LIMIT_MS, m
    assert m["http"]["p95_ms"] < DRY_RUN_P95_LIMIT_MS, m

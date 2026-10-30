"""NFR-1:路由決策耗時 P95 < 20 ms(不含上游呼叫)。

量測方式依 spec 11:100 個成員、當月 10,000 列帳本,連續 1,000 次 dry-run。與 AC-7 為同一量測
(`tests/acceptance/perf.py` 的 `dry_run_measurement()`,同一 pytest 行程內共用結果)。
經 HTTP 的量測(含序列化,較嚴)與直接呼叫 `Router.dry_run()` 的量測都必須 < 20 ms。
"""
from __future__ import annotations

import pytest

from tests.acceptance.perf import DRY_RUN_COUNT, DRY_RUN_P95_LIMIT_MS, dry_run_measurement

pytestmark = pytest.mark.anyio


async def test_nfr1_routing_decision_p95_under_20ms(open_spillway, fake_api, test_key_env):
    m = await dry_run_measurement(open_spillway, fake_api)
    assert m["http"]["n"] == m["router_direct"]["n"] == DRY_RUN_COUNT
    assert m["candidates"] + m["excluded"] == 100
    assert m["router_direct"]["p95_ms"] < DRY_RUN_P95_LIMIT_MS, m
    assert m["http"]["p95_ms"] < DRY_RUN_P95_LIMIT_MS, m

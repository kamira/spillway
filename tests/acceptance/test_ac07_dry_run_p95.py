"""AC-7:NFR-1 資料量(100 成員、當月 10,000 列帳本)下執行 1,000 次 dry-run,P95 < 20 ms。

量測程序見 `tests/acceptance/perf.py`(與 NFR-1 為同一量測,同一 pytest 行程內只跑一次、共用結果)。
"""
from __future__ import annotations

import pytest

from tests.acceptance.perf import DRY_RUN_COUNT, DRY_RUN_P95_LIMIT_MS, dry_run_measurement

pytestmark = pytest.mark.anyio


async def test_ac07_dry_run_p95_under_20ms(open_spillway, fake_api, test_key_env):
    m = await dry_run_measurement(open_spillway, fake_api)
    assert m["http"]["n"] == DRY_RUN_COUNT
    assert m["ledger_rows"] >= 10_000
    assert m["http"]["p95_ms"] < DRY_RUN_P95_LIMIT_MS, m

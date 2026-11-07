"""AC-7:NFR-1 資料量(100 成員、當月 10,000 列帳本)下執行 1,000 次 dry-run,P95 < 20 ms。

量測程序見 `tests/acceptance/perf.py`(與 NFR-1 為同一量測,同一 pytest 行程內只跑一次、共用結果;
黑箱:子行程 serve,經 HTTP 計時)。
"""
from __future__ import annotations

from tests.acceptance.perf import DRY_RUN_COUNT, DRY_RUN_P95_LIMIT_MS, dry_run_measurement


def test_ac07_dry_run_p95_under_20ms(serve_factory, fake_api, test_key_env):
    m = dry_run_measurement(serve_factory, fake_api)
    assert m["http"]["n"] == DRY_RUN_COUNT
    assert m["ledger_rows"] >= 10_000
    assert m["http"]["p95_ms"] < DRY_RUN_P95_LIMIT_MS, m

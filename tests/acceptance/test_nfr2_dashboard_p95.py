"""NFR-2:`/api/dashboard` P95 < 200 ms(資料量同 NFR-1);L1:種子產生器的資料量。

量測程序見 `tests/acceptance/perf.py`:種子資料後送一次 `/v1/chat/completions` 暖 local 健康快取,
暖機 10 次,再連續 200 次 `GET /api/dashboard`(黑箱:子行程 serve,經 HTTP 計時;門檻不變)。
種子產生器的檢查以唯讀連線讀資料庫(`Serve.db()`);「同一 seed 產生相同資料」以另一個資料目錄的 serve 再種一次比對。
"""
from __future__ import annotations

from datetime import timezone

from tests.acceptance.perf import (
    DASHBOARD_COUNT,
    DASHBOARD_P95_LIMIT_MS,
    dashboard_measurement,
    open_serve,
    prepare,
)
from tests.acceptance.seed import seed_nfr_dataset
from tests.blackbox.control import iso

_SAMPLE_SQL = "SELECT ts, deployment_id, status, tokens_out, cost_usd FROM usage_ledger ORDER BY id"


def test_seed_generator_exact_volume(serve_factory, fake_api, tmp_path):
    """L1:正好 100 個成員、10,000 列當月帳本;同一 seed 產生相同資料。"""
    h = open_serve(serve_factory, home=tmp_path / "first")
    now = h.control.now().astimezone(timezone.utc)
    ds = seed_nfr_dataset(h, now, fake_api.base_url)
    with h.db() as conn:
        n_members = conn.execute("SELECT COUNT(*) FROM deployments").fetchone()[0]
        month_start = iso(now.replace(day=1, hour=0, minute=0, second=0))
        n_month = conn.execute("SELECT COUNT(*) FROM usage_ledger WHERE ts >= ? AND ts <= ?",
                               (month_start, iso(now))).fetchone()[0]
        n_all = conn.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0]
        first = [tuple(r) for r in conn.execute(_SAMPLE_SQL)]
    assert len(ds.member_ids) == n_members == 100
    assert ds.ledger_rows == n_month == n_all == 10_000
    assert len(ds.local_ids) == 10
    h2 = open_serve(serve_factory, home=tmp_path / "again")
    seed_nfr_dataset(h2, now, fake_api.base_url)
    with h2.db() as conn:
        again = [tuple(r) for r in conn.execute(_SAMPLE_SQL)]
    assert again == first


def test_nfr2_dashboard_p95_under_200ms(serve_factory, fake_api, test_key_env):
    h = open_serve(serve_factory)
    prepare(h, fake_api.base_url)
    m = dashboard_measurement(h)
    assert m["members"] == 100
    assert m["http"]["n"] == DASHBOARD_COUNT
    assert m["http"]["p95_ms"] < DASHBOARD_P95_LIMIT_MS, m

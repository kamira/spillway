"""NFR-2:`/api/dashboard` P95 < 200 ms(資料量同 NFR-1);L1:種子產生器的資料量。

量測程序見 `tests/acceptance/perf.py`:種子資料後送一次 `/v1/chat/completions` 暖 local 健康快取,
暖機 10 次,再連續 200 次 `GET /api/dashboard`。
"""
from __future__ import annotations

from datetime import timezone

import pytest

from spillway.clock import to_iso
from tests.acceptance.perf import (
    DASHBOARD_COUNT,
    DASHBOARD_P95_LIMIT_MS,
    dashboard_measurement,
    prepare,
)
from tests.acceptance.seed import seed_nfr_dataset

pytestmark = pytest.mark.anyio

_SAMPLE_SQL = "SELECT ts, deployment_id, status, tokens_out, cost_usd FROM usage_ledger ORDER BY id"


async def test_seed_generator_exact_volume(open_spillway, fake_api):
    """L1:正好 100 個成員、10,000 列當月帳本;同一 seed 產生相同資料。"""
    async with open_spillway() as h:
        now = h.clock.now().astimezone(timezone.utc)
        with h.db() as conn:
            ds = seed_nfr_dataset(conn, now, fake_api.base_url)
            n_members = conn.execute("SELECT COUNT(*) FROM deployments").fetchone()[0]
            month_start = to_iso(now.replace(day=1, hour=0, minute=0, second=0))
            n_month = conn.execute("SELECT COUNT(*) FROM usage_ledger WHERE ts >= ? AND ts <= ?",
                                   (month_start, to_iso(now))).fetchone()[0]
            n_all = conn.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0]
            first = [tuple(r) for r in conn.execute(_SAMPLE_SQL)]
        assert len(ds.member_ids) == n_members == 100
        assert ds.ledger_rows == n_month == n_all == 10_000
        assert len(ds.local_ids) == 10
        again_path = h.home / "again.db"
    async with open_spillway(db_path=again_path) as h2:
        with h2.db() as conn:
            seed_nfr_dataset(conn, now, fake_api.base_url)
            again = [tuple(r) for r in conn.execute(_SAMPLE_SQL)]
    assert again == first


async def test_nfr2_dashboard_p95_under_200ms(open_spillway, fake_api, test_key_env):
    async with open_spillway() as h:
        await prepare(h, fake_api.base_url)
        m = await dashboard_measurement(h)
    assert m["members"] == 100
    assert m["http"]["n"] == DASHBOARD_COUNT
    assert m["http"]["p95_ms"] < DASHBOARD_P95_LIMIT_MS, m

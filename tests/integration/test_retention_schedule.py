"""RetentionJob 排程(CHG-20261030-02 T13;spec 2.4.5、NFR-5;design D4)。

app lifespan 啟動時先清理一次,之後每經過 Clock 上的 `retention_interval_seconds`(預設 3600)
清理一次;背景工作每 `retention_poll_seconds`(真實時間)檢查一次 Clock。
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from spillway.app import DEFAULT_RETENTION_INTERVAL_SECONDS
from spillway.store import repo

pytestmark = pytest.mark.anyio

POLL = 0.01


def _insert_payload(h, ts) -> int:
    with h.db() as conn:
        lid = repo.insert_ledger(conn, repo.LedgerRow(ts=ts, status="ok", deployment_id="x"))
        repo.insert_payload(conn, repo.Payload(ledger_id=lid, ts=ts, request_json="[]", bytes=2))
    return lid


def _has_payload(h, lid) -> bool:
    with h.db() as conn:
        return repo.get_payload(conn, lid) is not None


async def _wait_runs(h, n: int, timeout: float = 3.0) -> None:
    for _ in range(int(timeout / POLL)):
        if h.app.state.retention.runs >= n:
            return
        await asyncio.sleep(POLL)
    raise AssertionError(f"排程沒有在 {timeout}s 內達到第 {n} 次執行(目前 {h.app.state.retention.runs})")


def test_default_interval_is_one_hour():
    assert DEFAULT_RETENTION_INTERVAL_SECONDS == 3600


async def test_runs_at_startup(open_spillway, clock):
    async with open_spillway(retention_poll_seconds=POLL) as h:
        assert h.app.state.retention.runs == 1


async def test_advancing_clock_one_hour_triggers_one_cleanup(open_spillway, clock):
    async with open_spillway(retention_poll_seconds=POLL) as h:
        with h.db() as conn:
            repo.set_setting(conn, "context_retention_days", 1)
        await _wait_runs(h, 1)
        old = _insert_payload(h, clock.now() - timedelta(days=2))   # 已過期
        fresh = _insert_payload(h, clock.now())

        # 時間沒前進:多輪輪詢都不清理
        await asyncio.sleep(POLL * 20)
        assert h.app.state.retention.runs == 1
        assert _has_payload(h, old)

        clock.advance(minutes=59)
        await asyncio.sleep(POLL * 20)
        assert h.app.state.retention.runs == 1
        assert _has_payload(h, old)

        clock.advance(minutes=1)          # 滿一小時
        await _wait_runs(h, 2)
        await asyncio.sleep(POLL * 20)
        assert h.app.state.retention.runs == 2   # 恰好一次
        assert not _has_payload(h, old)
        assert _has_payload(h, fresh)
        with h.db() as conn:                       # 帳本列保留
            assert repo.get_ledger(conn, old) is not None


async def test_job_stops_on_shutdown(open_spillway, clock):
    async with open_spillway(retention_poll_seconds=POLL) as h:
        task = h.app.state.retention.task
        assert task is not None and not task.done()
    assert task.done()


async def test_cleanup_error_does_not_kill_scheduler(open_spillway, clock, monkeypatch, caplog):
    from spillway.ledger import RetentionJob

    calls = {"n": 0}
    real = RetentionJob.run_once

    def flaky(self, now=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return real(self, now)

    monkeypatch.setattr(RetentionJob, "run_once", flaky)
    async with open_spillway(retention_poll_seconds=POLL) as h:
        clock.advance(hours=1)
        await asyncio.sleep(POLL * 20)
        clock.advance(hours=1)
        await _wait_runs(h, 2)
        assert calls["n"] >= 3
        assert not h.app.state.retention.task.done()
    assert "RuntimeError" in caplog.text

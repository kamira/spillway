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


# ---- CHG-20261030-02 T13/T14 審議 M22:最壞延遲 ≤ interval(NFR-5)與排程韌性 ----


async def test_triggers_within_one_poll_after_interval_minus_poll(open_spillway, clock):
    async with open_spillway(retention_poll_seconds=POLL) as h:
        assert h.app.state.retention.runs == 1
        clock.advance(seconds=3600 - POLL)
        await _wait_runs(h, 2, timeout=POLL * 50)
        assert h.app.state.retention.runs == 2


def test_worst_case_delay_is_at_most_interval(clock):
    from spillway.app import RetentionScheduler

    sched = RetentionScheduler(lambda: None, clock, interval_seconds=3600, poll_seconds=30)
    sched.next_due = clock.now() + timedelta(seconds=3600)
    clock.advance(seconds=3600 - 30 - 1)
    assert not sched.due()
    clock.advance(seconds=1)          # 3600 − poll:本輪就清理,不必等到下一輪(3600 + poll)
    assert sched.due()


class _FlakyClock:
    def __init__(self, inner, fail_on: set[int]):
        self.inner, self.fail_on, self.n = inner, fail_on, 0

    def now(self):
        self.n += 1
        if self.n in self.fail_on:
            raise RuntimeError("clock boom")
        return self.inner.now()


async def test_clock_error_does_not_kill_scheduler(clock, caplog):
    from spillway.app import RetentionScheduler

    flaky = _FlakyClock(clock, fail_on={2, 3})
    calls = []
    sched = RetentionScheduler(lambda: calls.append(1), flaky, interval_seconds=3600,
                               poll_seconds=POLL)
    sched.next_due = clock.now()
    sched.task = asyncio.get_running_loop().create_task(sched._loop())
    for _ in range(100):
        if flaky.n > 4:
            break
        await asyncio.sleep(POLL)
    assert not sched.task.done()
    await sched.stop()
    assert "RuntimeError" in caplog.text
    assert "clock boom" not in caplog.text


async def test_stop_swallows_non_cancel_exception(clock):
    from spillway.app import RetentionScheduler

    sched = RetentionScheduler(lambda: None, clock, poll_seconds=POLL)

    async def boom():
        raise ValueError("x")

    sched.task = asyncio.get_running_loop().create_task(boom())
    await asyncio.sleep(0)
    await sched.stop()          # 不往外丟
    assert sched.task.done()

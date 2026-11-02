"""`spillway.api.drain`(CHG-20261102-01 T7;決策 14 ②、18)。

`run_until_drain`:完成時回結果、例外原樣往上丟;`drain_expired` 到期時兩次 cancel 後回 `Drained`
(子 task 不理會取消也照樣回,留在登記表);外層取消轉發給子 task。`ShutdownHooks`:預設為空、
`fire()` 只生效一次、單一掛鉤失敗不影響其他掛鉤、async 掛鉤排成 task。
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from spillway.api import drain
from spillway.api.drain import Drained, ShutdownHooks, run_until_drain

pytestmark = pytest.mark.anyio


def _app() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(drain_expired=asyncio.Event(), drain_tasks=set()))


async def test_completes_returns_result_and_unregisters():
    app = _app()

    async def work() -> str:
        await asyncio.sleep(0.01)
        return "ok"

    assert await run_until_drain(app, work()) == "ok"
    await asyncio.sleep(0)
    assert app.state.drain_tasks == set()


async def test_child_exception_propagates():
    app = _app()

    async def boom() -> None:
        raise LookupError("x")

    with pytest.raises(LookupError):
        await run_until_drain(app, boom())


async def test_child_is_registered_while_running():
    app = _app()
    gate = asyncio.Event()

    async def work() -> int:
        await gate.wait()
        return 1

    outer = asyncio.ensure_future(run_until_drain(app, work()))
    await asyncio.sleep(0.01)
    assert len(app.state.drain_tasks) == 1
    gate.set()
    assert await outer == 1


async def test_expired_cancels_child_once_when_it_finishes_promptly():
    app = _app()
    cancels: list[float] = []

    async def work() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancels.append(time.monotonic())
            raise

    outer = asyncio.ensure_future(run_until_drain(app, work()))
    await asyncio.sleep(0.01)
    start = time.monotonic()
    app.state.drain_expired.set()
    result = await asyncio.wait_for(outer, 2)
    assert isinstance(result, Drained)
    assert len(cancels) == 1
    assert time.monotonic() - start < drain.FIRST_CANCEL_WAIT + 0.1


async def test_expired_cancels_twice_then_returns_drained_even_if_child_lingers():
    """子 task 吞掉取消(模擬 CliRunner 的 shielded 收尾):0.2 秒後第二次 cancel,再等 ≤ 0.5 秒就回
    `Drained`;子 task 留在登記表。"""
    app = _app()
    cancels: list[float] = []
    release = asyncio.Event()

    async def stubborn() -> None:
        while not release.is_set():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancels.append(time.monotonic())

    outer = asyncio.ensure_future(run_until_drain(app, stubborn()))
    await asyncio.sleep(0.01)
    start = time.monotonic()
    app.state.drain_expired.set()
    result = await asyncio.wait_for(outer, 3)
    elapsed = time.monotonic() - start
    assert isinstance(result, Drained)
    assert len(cancels) == 2
    assert cancels[1] - cancels[0] == pytest.approx(drain.FIRST_CANCEL_WAIT, abs=0.1)
    assert elapsed == pytest.approx(drain.FIRST_CANCEL_WAIT + drain.SECOND_CANCEL_WAIT, abs=0.15)
    (child,) = app.state.drain_tasks                      # 未收尾的子 task 留給 lifespan
    assert not child.done()
    release.set()
    child.cancel()
    await asyncio.wait({child}, timeout=1)
    assert child.done()
    await asyncio.sleep(0)
    assert app.state.drain_tasks == set()


async def test_expired_before_start_still_runs_cancel_path():
    app = _app()
    app.state.drain_expired.set()

    async def work() -> str:
        await asyncio.sleep(60)
        return "never"

    assert isinstance(await run_until_drain(app, work()), Drained)


async def test_outer_cancel_is_forwarded_to_child():
    app = _app()
    seen: list[str] = []

    async def work() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            seen.append("child cancelled")
            raise

    outer = asyncio.ensure_future(run_until_drain(app, work()))
    await asyncio.sleep(0.01)
    (child,) = app.state.drain_tasks
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    await asyncio.wait({child}, timeout=1)
    assert child.cancelled()
    assert seen == ["child cancelled"]


def test_shutdown_hooks_default_empty_and_fire_once():
    hooks = ShutdownHooks()
    assert list(hooks) == []
    hooks.fire()                                          # 空清單:不做任何事
    calls: list[str] = []
    hooks2 = ShutdownHooks()
    hooks2.append(lambda: calls.append("a"))
    hooks2.append(lambda: 1 / 0)                          # 失敗的掛鉤不影響後面的
    hooks2.append(lambda: calls.append("b"))
    hooks2.fire()
    hooks2.fire()
    assert calls == ["a", "b"]


async def test_shutdown_hooks_async_callback_is_scheduled():
    done = asyncio.Event()

    async def hook() -> None:
        done.set()

    hooks = ShutdownHooks([hook])
    hooks.fire()
    await asyncio.wait_for(done.wait(), 1)


def test_create_app_initialises_drain_state(tmp_path):
    from spillway.app import create_app

    app = create_app(db_path=tmp_path / "s.db")
    assert isinstance(app.state.drain_expired, asyncio.Event)
    assert not app.state.drain_expired.is_set()
    assert app.state.drain_tasks == set()
    assert isinstance(app.state.shutdown_hooks, ShutdownHooks) and list(app.state.shutdown_hooks) == []

"""停機期滿的 drain helper 與停機掛鉤(CHG-20261102-01 T7;決策 14 ②、18)。

`async def run_until_drain(app, coro) -> T | Drained`
- 把 `coro` 包成子 task,登記在 `app.state.drain_tasks`(set;完成後自動移除)。
- 等待「子 task 完成」或 `app.state.drain_expired`(`asyncio.Event`)被設定,先到者為準:
  - 子 task 先完成 → 回傳它的結果(例外原樣往上丟)。
  - `drain_expired` 先到 → `child.cancel()` → 等 `FIRST_CANCEL_WAIT`(0.2 秒)→ 未結束再 `child.cancel()`
    (CliRunner 把重複取消視為「同步 SIGKILL 整個 group」)→ 再等最多 `SECOND_CANCEL_WAIT`(0.5 秒)→
    不論子 task 是否收尾完畢都回 `Drained()`。未結束的子 task 留在登記表,由 `cli.py` 在 lifespan 結束前的
    有限等待(本模組的 `CANCELLED_DRAIN_TIMEOUT`,`cli.py` 與 `app.py` 共用)收尾;T3 的 lifespan 關閉順序第 2 步也會等它們。
- 外層(呼叫端)被取消時:`finally` 取消子 task,把取消轉發下去;`CancelledError` 照常往上丟。

呼叫端:`api/openai.py` 的 `/v1/chat/completions`(`Drained` → 503 `shutting_down`)與 MCP `ask`
(`Drained` → `CallToolResult(isError=true)`)。

`drain_expired` 由 `spillway serve` 的訊號處理在第一個訊號時以
`loop.call_later(max(grace*0.5, grace-1.5), ev.set)` 排定(見 `cli.py`);行程內的 app 預設永不設定,
`run_until_drain` 等同直接 await `coro`。未結束的子 task 由 `cli.py` 在 lifespan 結束前有限等待
(另見 T3 的 lifespan 關閉順序第 2 步)。寬限小於約 1.2 秒(僅測試用範圍)時 uvicorn 的期滿取消可能先到。

`ShutdownHooks`:`app.state.shutdown_hooks` 的型別——回呼清單(`list` 子類別,`append` 登記),
`fire()` 依序呼叫每個零參數回呼;回呼回傳 awaitable 時排成 task。`fire()` 只生效一次;單一回呼的例外
只記例外型別,不影響其他回呼。由 `serve` 第一個訊號時以 `loop.call_soon_threadsafe(hooks.fire)` 觸發。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, TypeVar

from spillway.adapters.base import DEFAULT_DRAIN_TIMEOUT, DEFAULT_TERM_GRACE

__all__ = ["Drained", "ShutdownHooks", "run_until_drain", "FIRST_CANCEL_WAIT", "SECOND_CANCEL_WAIT",
           "CANCELLED_DRAIN_TIMEOUT"]

_log = logging.getLogger(__name__)

T = TypeVar("T")

FIRST_CANCEL_WAIT = 0.2    # 第一次 cancel 後等子 task 走完取消路徑的時間(秒)
SECOND_CANCEL_WAIT = 0.5   # 第二次 cancel(CliRunner 同步 SIGKILL)後的等待上限(秒)
# 被取消的請求/子 task 走完 CliRunner 取消路徑的等待上限:SIGTERM 寬限 + 讀管線上限 + 1 秒
# (`cli.py` 期滿取消後的等待與 `app.py` lifespan 關閉第 2 步共用)
CANCELLED_DRAIN_TIMEOUT = DEFAULT_TERM_GRACE + DEFAULT_DRAIN_TIMEOUT + 1.0


class Drained:
    """`run_until_drain` 的回傳值:停機期滿,子 task 已被取消(可能尚未收尾完畢)。"""

    __slots__ = ()

    def __repr__(self) -> str:
        return "Drained()"


class ShutdownHooks(list):
    """停機掛鉤清單(決策 14 ②);預設為空。"""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.fired = False
        self._tasks: set[asyncio.Task[Any]] = set()

    def fire(self) -> None:
        if self.fired:
            return
        self.fired = True
        for hook in list(self):
            try:
                result = hook()
                if inspect.isawaitable(result):
                    task = asyncio.ensure_future(_guard(result))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
            except Exception as exc:  # noqa: BLE001 — 單一掛鉤失敗不影響其他掛鉤與停機
                _log_hook_failure(exc)


def _log_hook_failure(exc: BaseException) -> None:
    _log.error("停機掛鉤失敗:%s", type(exc).__name__)   # 只記例外型別


async def _guard(aw: Awaitable[Any]) -> None:
    try:
        await aw
    except Exception as exc:  # noqa: BLE001 — async 掛鉤失敗同樣只記錄,不影響停機
        _log_hook_failure(exc)


def _retrieve(task: asyncio.Future[Any]) -> None:
    """子 task 結束時取出例外,避免 "exception was never retrieved"(Drained 後沒有人 await 它)。"""
    if not task.cancelled():
        task.exception()


async def run_until_drain(app: Any, coro: Coroutine[Any, Any, T]) -> T | Drained:
    state = app.state
    registry: set[asyncio.Task[Any]] = state.drain_tasks
    expired: asyncio.Event = state.drain_expired
    child: asyncio.Task[T] = asyncio.ensure_future(coro)
    registry.add(child)
    child.add_done_callback(registry.discard)
    child.add_done_callback(_retrieve)
    waiter = asyncio.ensure_future(expired.wait())
    drained = False
    try:
        await asyncio.wait({child, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if child.done():
            return child.result()
        drained = True
        child.cancel(msg="shutdown drain expired")
        await asyncio.wait({child}, timeout=FIRST_CANCEL_WAIT)
        if not child.done():
            child.cancel(msg="shutdown drain expired")
            await asyncio.wait({child}, timeout=SECOND_CANCEL_WAIT)
        return Drained()
    finally:
        waiter.cancel()
        if not drained and not child.done():   # 外層被取消(或例外):轉發取消給子 task
            child.cancel()

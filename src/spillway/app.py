"""FastAPI app 組裝(CHG-20261030-02 T13 擁有;design D1、D2、D4;spec 2.4.5、7.6.6)。

`create_app(*, clock=None, rng=None, extra_adapter_paths=(), db_path=None,
retention_interval_seconds=3600, retention_poll_seconds=30.0) -> FastAPI`

lifespan 啟動依序:
1. `load_litellm()`:本地價格表(`LITELLM_LOCAL_MODEL_COST_MAP=True`)、關閉 verbose 與遙測。
2. 建立資料目錄,`store.db.connect(db_path)` 一次以執行 migration(隨即關閉)。
3. `AdapterRegistry(AdapterContext(credentials.resolve, clock)).discover(extra_adapter_paths)`;
   `AdapterConflictError`(7.6.6)直接往上丟 → 啟動失敗(`spillway serve` 以非 0 退出)。
4. `Router(conn_factory, registry, clock, rng, close_conn=True)`:每個請求開一條新連線、結束時關閉。
5. RetentionJob 排程(`RetentionScheduler`):啟動時先清理一次,之後每經過 Clock 上的
   `retention_interval_seconds` 清理一次;背景工作每 `retention_poll_seconds`(真實時間)檢查一次
   Clock,所以 `FakeClock.advance()` 就能觸發。清理失敗只記例外型別,排程繼續。
關閉時停止排程工作。

`app.state`(T14 管理端點與 T15 使用):
- `clock`:`Clock`(預設 `SystemClock()`)
- `rng`:`Rng`(預設 `SystemRng()`)
- `db_path`:`pathlib.Path`
- `conn_factory`:零參數函式,每次呼叫開一條**新**連線(已 migration;呼叫端負責 `close()`)
- `registry`:已 `discover()` 的 `AdapterRegistry`
- `router`:`spillway.routing.Router`(其 `quota`、`estimator` 只在 route()/dry_run() 內可用)
- `retention`:`RetentionScheduler`(`runs` 成功次數、`task`、`run_once()`)
`registry`、`router`、`retention` 在 lifespan 啟動後才存在。

秘密:本檔不記錄請求內容;日誌只記例外型別。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI

from spillway import credentials, settings
from spillway.adapters.base import AdapterContext, AdapterRegistry, load_litellm
from spillway.api import install_error_handlers
from spillway.api import openai as openai_api
from spillway.clock import Clock, Rng, SystemClock, SystemRng
from spillway.ledger import RetentionJob
from spillway.routing import Router
from spillway.store.db import connect

__all__ = ["create_app", "RetentionScheduler", "DEFAULT_RETENTION_INTERVAL_SECONDS",
           "DEFAULT_RETENTION_POLL_SECONDS"]

_log = logging.getLogger(__name__)

DEFAULT_RETENTION_INTERVAL_SECONDS = 3600     # spec 2.4.5「每小時」
DEFAULT_RETENTION_POLL_SECONDS = 30.0         # 真實時間的檢查間隔;實際清理延遲 ≤ 1 小時 + 30 秒

ConnFactory = Callable[[], sqlite3.Connection]


class RetentionScheduler:
    """以注入的 Clock 計時、定期執行 `RetentionJob.run_once()`。

    每次清理開一條新連線、用完關閉。`runs` 為成功次數;`next_due` 為下一次應執行的 Clock 時間。
    """

    def __init__(self, conn_factory: ConnFactory, clock: Clock, *,
                 interval_seconds: float = DEFAULT_RETENTION_INTERVAL_SECONDS,
                 poll_seconds: float = DEFAULT_RETENTION_POLL_SECONDS) -> None:
        if interval_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("retention 間隔必須為正數")
        self._factory = conn_factory
        self._clock = clock
        self.interval = timedelta(seconds=interval_seconds)
        self.poll_seconds = poll_seconds
        self.runs = 0
        self.next_due: datetime | None = None
        self.task: asyncio.Task[None] | None = None

    def run_once(self) -> int | None:
        """清理一次;回傳刪除列數,失敗時回 None(只記例外型別)。無論成敗都排定下一次。"""
        now = self._clock.now()
        self.next_due = now + self.interval
        try:
            conn = self._factory()
            try:
                deleted = RetentionJob(conn, self._clock).run_once(now)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 — 清理失敗不可讓排程停止
            _log.error("RetentionJob 執行失敗:%s", type(exc).__name__)
            return None
        self.runs += 1
        return deleted

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_seconds)
            if self.next_due is None or self._clock.now() >= self.next_due:
                self.run_once()

    def start(self) -> None:
        self.run_once()
        self.task = asyncio.get_running_loop().create_task(self._loop(), name="spillway-retention")

    async def stop(self) -> None:
        task, self.task = self.task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self.task = task   # 保留參照,讓呼叫端可確認已結束


def create_app(
    *,
    clock: Clock | None = None,
    rng: Rng | None = None,
    extra_adapter_paths: Iterable[Path | str] = (),
    db_path: Path | str | None = None,
    retention_interval_seconds: float = DEFAULT_RETENTION_INTERVAL_SECONDS,
    retention_poll_seconds: float = DEFAULT_RETENTION_POLL_SECONDS,
) -> FastAPI:
    clk: Clock = clock if clock is not None else SystemClock()
    rnd: Rng = rng if rng is not None else SystemRng()
    path = Path(db_path) if db_path is not None else settings.db_path()
    extra = tuple(extra_adapter_paths)

    def conn_factory() -> sqlite3.Connection:
        return connect(path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        load_litellm()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn_factory().close()   # migration(D3)
        registry = AdapterRegistry(AdapterContext(credentials=credentials.resolve, clock=clk))
        registry.discover(extra)   # AdapterConflictError → 啟動失敗(7.6.6)
        app.state.registry = registry
        app.state.router = Router(conn_factory, registry, clk, rnd, close_conn=True)
        retention = RetentionScheduler(conn_factory, clk, interval_seconds=retention_interval_seconds,
                                       poll_seconds=retention_poll_seconds)
        app.state.retention = retention
        retention.start()
        try:
            yield
        finally:
            await retention.stop()

    app = FastAPI(title="spillway", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.clock = clk
    app.state.rng = rnd
    app.state.db_path = path
    app.state.conn_factory = conn_factory
    install_error_handlers(app)

    app.include_router(openai_api.router)
    # ---- T15(CHG-20261030-02)只在此處追加下一行掛載管理端點,其餘不動 ----
    # app.include_router(admin.router)   # 需要 `from spillway.api import admin`
    # ---- T15 追加處結束 ----
    return app

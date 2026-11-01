"""FastAPI app 組裝(CHG-20261030-02 T13 擁有;design D1、D2、D4;spec 2.4.5、7.6.6)。

`create_app(*, clock=None, rng=None, extra_adapter_paths=(), db_path=None,
retention_interval_seconds=3600, retention_poll_seconds=30.0, issue_token=False) -> FastAPI`

lifespan 啟動依序:
1. `load_litellm()`:本地價格表(`LITELLM_LOCAL_MODEL_COST_MAP=True`)、關閉 verbose 與遙測。
2. 建立資料目錄,`store.db.connect(db_path)` 一次以執行 migration(隨即關閉)。
   `issue_token=True`(`spillway serve` 傳入;CHG-20261101-03 決策 14)時接著 `_ensure_token(app)`:
   資料庫沒有 `access_token_sha256` 就產生 token、只存雜湊,並以單行(`TOKEN_NOTICE` + token)印到
   stderr——只印這一次,不經 logging。行程內使用 `create_app()` 的測試預設不產生。
3. `AdapterRegistry(AdapterContext(credentials.resolve, clock)).discover(extra_adapter_paths)`;
   `AdapterConflictError`(7.6.6)直接往上丟 → 啟動失敗(`spillway serve` 以非 0 退出)。
4. `Router(conn_factory, registry, clock, rng, close_conn=True)`:每個請求開一條新連線、結束時關閉。
5. RetentionJob 排程(`RetentionScheduler`):啟動時先清理一次,之後每經過 Clock 上的
   `retention_interval_seconds` 清理一次;背景工作每 `retention_poll_seconds`(真實時間)檢查一次
   Clock,所以 `FakeClock.advance()` 就能觸發。到期判斷提前兩個輪詢間隔(`now >= next_due - 2 × poll`,
   CHG-20261101-02 決策 14):輪詢抖動(sleep 超時、清理耗時)≤ poll 時,兩次清理的間隔 ≤ interval
   (NFR-5:1 小時內),最多提早 2 × poll;清理本身耗時超過 poll 的情況不在此上界內。
   清理失敗或讀 Clock 失敗只記例外型別,排程繼續。
關閉時停止排程工作。

`app.state`(T14 管理端點與 T15 使用):
- `clock`:`Clock`(預設 `SystemClock()`)
- `rng`:`Rng`(預設 `SystemRng()`)
- `db_path`:`pathlib.Path`
- `conn_factory`:零參數函式,每次呼叫開一條**新**連線(已 migration;呼叫端負責 `close()`)
- `registry`:已 `discover()` 的 `AdapterRegistry`
- `router`:`spillway.routing.Router`(其 `quota`、`estimator` 只在 route()/dry_run() 內可用)
- `retention`:`RetentionScheduler`(`runs` 成功次數、`task`、`run_once()`)
- `cli_runner`:`CliRunner`(T2);`shutting_down`:停機旗標,預設 False(CHG-20261101-03 決策 16)
`registry`、`router`、`retention` 在 lifespan 啟動後才存在。

秘密:本檔不記錄請求內容;日誌只記例外型別。存取 token 明文只出現在 `_ensure_token` 的 stderr 一行。
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI

from spillway import credentials, settings
from spillway.adapters.base import AdapterContext, AdapterRegistry, CliRunner, load_litellm
from spillway.api import install_error_handlers
from spillway.api import admin
from spillway.api import openai as openai_api
from spillway.clock import Clock, Rng, SystemClock, SystemRng
from spillway.ledger import RetentionJob
from spillway.routing import Router
from spillway.store.db import connect

__all__ = ["create_app", "RetentionScheduler", "DEFAULT_RETENTION_INTERVAL_SECONDS",
           "DEFAULT_RETENTION_POLL_SECONDS", "TOKEN_NOTICE"]

# serve 首次啟動印到 stderr 的單行前綴(後接 token 明文;決策 14)
TOKEN_NOTICE = "spillway: 已產生存取 token(只顯示這一次,資料庫只存雜湊): "

_log = logging.getLogger(__name__)

DEFAULT_RETENTION_INTERVAL_SECONDS = 3600     # spec 2.4.5「每小時」
DEFAULT_RETENTION_POLL_SECONDS = 30.0         # 真實時間的檢查間隔;到期判斷提前 2 × poll,延遲 ≤ 1 小時

ConnFactory = Callable[[], sqlite3.Connection]


class RetentionScheduler:
    """以注入的 Clock 計時、定期執行 `RetentionJob.run_once()`。

    每次清理開一條新連線、用完關閉。`runs` 為成功次數;`next_due` 為上次清理時間 + interval。
    背景輪詢在 `now >= next_due - 2 × poll_seconds` 時清理(`due()`);輪詢抖動 ≤ poll 時,
    兩次清理的間隔 ≤ interval。
    輪詢中的任何例外(含 Clock 失敗)只記例外型別,排程繼續;`stop()` 吞掉非取消的例外。
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

    def due(self) -> bool:
        """是否該清理:`now >= next_due - 2 × poll`(CHG-20261101-02 決策 14)。

        下一次輪詢最晚在 poll + 抖動之後;提前兩個輪詢間隔,所以輪詢抖動 ≤ poll 時,兩次清理的
        間隔 ≤ interval(只提前一個 poll 時,多睡一個 poll 就會超過 interval)。最多提早 2 × poll。"""
        if self.next_due is None:
            return True
        return self._clock.now() >= self.next_due - timedelta(seconds=2 * self.poll_seconds)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_seconds)
            try:
                if self.due():
                    self.run_once()
            except Exception as exc:  # noqa: BLE001 — 例如 Clock 失敗;排程不可因此終止
                _log.error("RetentionScheduler 輪詢失敗:%s", type(exc).__name__)

    def start(self) -> None:
        self.run_once()
        self.task = asyncio.get_running_loop().create_task(self._loop(), name="spillway-retention")

    async def stop(self) -> None:
        task, self.task = self.task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 — 關閉時不讓排程的例外中斷 lifespan
            _log.error("RetentionScheduler 結束時發生例外:%s", type(exc).__name__)
        self.task = task   # 保留參照,讓呼叫端可確認已結束


def _ensure_token(app: FastAPI) -> None:
    """資料庫沒有存取 token 雜湊時產生一個,並把明文以單行印到 stderr(只此一次;CHG-20261101-03
    決策 14)。不經 logging,所以不會進入日誌檔或 caplog。"""
    conn = app.state.conn_factory()
    try:
        token = settings.ensure_access_token(conn)
    finally:
        conn.close()
    if token is not None:
        print(TOKEN_NOTICE + token, file=sys.stderr, flush=True)


def create_app(
    *,
    clock: Clock | None = None,
    rng: Rng | None = None,
    extra_adapter_paths: Iterable[Path | str] = (),
    db_path: Path | str | None = None,
    retention_interval_seconds: float = DEFAULT_RETENTION_INTERVAL_SECONDS,
    retention_poll_seconds: float = DEFAULT_RETENTION_POLL_SECONDS,
    issue_token: bool = False,
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
        if issue_token:   # CHG-20261101-03 T7(決策 14):只有 serve 傳入
            _ensure_token(app)
        runner = app.state.cli_runner   # 由 create_app 在 lifespan 之前建立(T2;停機路徑也用它)
        registry = AdapterRegistry(AdapterContext(credentials=credentials.resolve, clock=clk,
                                                  cli_runner=runner, data_dir=path.parent))
        registry.discover(extra)   # AdapterConflictError → 啟動失敗(7.6.6)
        app.state.registry = registry
        # CHG-20261101-03 T6(決策 7、9、16):名額與 env 清理用同一個 CliRunner;停機旗標讀 app.state
        app.state.router = Router(conn_factory, registry, clk, rnd, close_conn=True, cli_runner=runner,
                                  shutdown_flag=lambda: app.state.shutting_down)
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
    # CHG-20261101-03 T2(決策 8):必須在 lifespan 之前存在;lifespan 注入 AdapterContext.cli_runner
    app.state.cli_runner = CliRunner()
    app.state.shutting_down = False   # T6(決策 16):T8 的訊號處理設為 True;Router 不再開始新嘗試
    app.state.db_path = path
    app.state.conn_factory = conn_factory
    install_error_handlers(app)

    app.include_router(openai_api.router)
    # ---- T15(CHG-20261030-02)只在此處追加下一行掛載管理端點,其餘不動 ----
    app.include_router(admin.router)
    # ---- T15 追加處結束 ----
    return app

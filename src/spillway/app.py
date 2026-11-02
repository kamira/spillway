"""FastAPI app 組裝(CHG-20261030-02 T13 擁有;design D1、D2、D4;spec 2.4.5、7.6.6)。

`create_app(*, clock=None, rng=None, extra_adapter_paths=(), db_path=None,
retention_interval_seconds=3600, retention_poll_seconds=30.0, issue_token=False, port=8787) -> FastAPI`

`port`(CHG-20261102-01 T1,決策 4):serve 綁定的埠,存為 `app.state.port`,供 Host/Origin 檢查
比對 `Origin`;`spillway serve` 以實際埠傳入。最外層掛 `HostOriginMiddleware`(純 ASGI):每個
`http`/`websocket` 請求的 Host 必須是 127.0.0.1 或 localhost(403 `host_not_allowed`),帶 `Origin`
時必須等於 `http://127.0.0.1:<port>` 或 `http://localhost:<port>`(403 `origin_not_allowed`)。
不變式(D33):所有 GET 端點不得改變狀態。

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
- `port`:serve 綁定埠(CHG-20261102-01 T1)
- `cli_runner`:`CliRunner`(T2);`shutting_down`:停機旗標,預設 False(CHG-20261101-03 決策 16)
- `drain_expired`(`asyncio.Event`)、`drain_tasks`(set)、`shutdown_hooks`(`api.drain.ShutdownHooks`,
  預設為空):CHG-20261102-01 決策 14 ②、18;由 `spillway serve` 的訊號處理設定/觸發
- `mcp_server`(`api.mcp.SpillwayServer`)、`mcp_manager`(SDK `StreamableHTTPSessionManager`;只在
  lifespan 內存在,每次進 lifespan 新建,外面為 None)、`mcp_events`(`api.mcp.events.McpEvents`:
  `register(session)`、同步的 `changed()` 送 `notifications/tools/list_changed`;T3):
  CHG-20261102-01 決策 9、13。`/mcp/<group>` 由 `api.mcp.McpEndpoint` 掛載。lifespan 啟動 MCP session
  manager 後在 `shutdown_hooks` 登記 MCP 停機掛鉤(`api.mcp.server.install_shutdown_hook`:第一個訊號時
  只關每個 session 的 GET 通知串流;決策 14 ③),lifespan 結束時移除。
`registry`、`router`、`retention` 在 lifespan 啟動後才存在。
lifespan 在 RetentionJob 排程之後以 `api.mcp.McpRuntime.start(app)` 啟動 MCP session manager 的專用持有
task。關閉順序(D31;CHG-20261102-01 決策 14、AM-6):
1. 停 manager 持有 task(set 停止事件並 await `mgr.run()` 結束:`/mcp` 之後回 503,取消剩餘 session 與
   工具 task——`run_until_drain` 把取消轉給子 task,Router 寫 `shutdown`、CliRunner 殺 group);
2. 有上限地等待 `app.state.drain_tasks`(`api.drain.CANCELLED_DRAIN_TIMEOUT`;逾時再取消一次並短暫等待);
3. `retention.stop()`;
4. `cli_runner.terminate_all`(SIGTERM→5 秒→SIGKILL 的後備)。

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
from spillway.adapters.base import (
    DEFAULT_TERM_GRACE,
    AdapterContext,
    AdapterRegistry,
    CliRunner,
    load_litellm,
)
from spillway.api import install_error_handlers
from spillway.api import admin
from spillway.api import openai as openai_api
from spillway.api.host_origin import HostOriginMiddleware
from spillway.api.mcp import McpEndpoint, McpRuntime, SpillwayServer
from spillway.api.mcp.events import McpEvents
from spillway.api.mcp.sdk_logging import install_sdk_root_log_filter
from spillway.api.mcp.server import MAX_SESSIONS, install_shutdown_hook
from spillway.api.drain import CANCELLED_DRAIN_TIMEOUT, SECOND_CANCEL_WAIT, ShutdownHooks
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


async def wait_drain_tasks(app: FastAPI, timeout: float = CANCELLED_DRAIN_TIMEOUT) -> None:
    """lifespan 關閉第 2 步(D31;決策 14、18):有上限地等待 `app.state.drain_tasks` 裡未結束的 task;
    逾時仍未結束者再取消一次(CliRunner 視重複取消為同步 SIGKILL)並等待至多 `SECOND_CANCEL_WAIT`。"""
    pending = {t for t in app.state.drain_tasks if not t.done()}
    if not pending:
        return
    _, still = await asyncio.wait(pending, timeout=timeout)
    if still:
        for task in still:
            task.cancel(msg="lifespan shutdown")
        await asyncio.wait(still, timeout=SECOND_CANCEL_WAIT)


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
    port: int = 8787,
    mcp_max_sessions: int = MAX_SESSIONS,   # CHG-20261102-02 決策 19:MCP session 上限
) -> FastAPI:
    clk: Clock = clock if clock is not None else SystemClock()
    rnd: Rng = rng if rng is not None else SystemRng()
    # CHG-20261102-01 終審修正 A:SDK 直接寫 root logger 的驗證失敗紀錄含 request 片段(可能是 prompt)
    install_sdk_root_log_filter()
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
        # CHG-20261102-01 T2(決策 9、AM-4):每次 lifespan 新建 MCP session manager 與專用持有 task
        try:
            mcp_runtime = await McpRuntime.start(app, max_sessions=mcp_max_sessions)
        except BaseException:
            await retention.stop()
            raise
        # CHG-20261102-01 T3(決策 14 ③):第一個訊號時只關每個 session 的 GET 通知串流
        remove_shutdown_hook = install_shutdown_hook(app)
        try:
            yield
        finally:
            remove_shutdown_hook()
            # 關閉順序第 1 步(D31):停 manager 持有 task(之後 `/mcp` 回 503;取消剩餘 session 與工具 task)
            await mcp_runtime.stop()
            # 第 2 步(CHG-20261102-01 決策 14、18):有上限地等待 drain 登記表
            await wait_drain_tasks(app)
            await retention.stop()
            # CHG-20261101-03 T8(決策 16 ③):停機後備,SIGTERM→5 秒→SIGKILL
            await runner.terminate_all(grace=DEFAULT_TERM_GRACE)

    app = FastAPI(title="spillway", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.clock = clk
    app.state.rng = rnd
    # CHG-20261101-03 T2(決策 8):必須在 lifespan 之前存在;lifespan 注入 AdapterContext.cli_runner
    app.state.cli_runner = CliRunner()
    app.state.shutting_down = False   # T6(決策 16):T8 的訊號處理設為 True;Router 不再開始新嘗試
    # CHG-20261102-01 T7(決策 14 ②、18):停機期滿事件、drain 登記表、停機掛鉤(預設為空)
    app.state.drain_expired = asyncio.Event()
    app.state.drain_tasks = set()
    app.state.shutdown_hooks = ShutdownHooks()
    # CHG-20261102-01 T2(決策 9、13):MCP server 實例;manager 只在 lifespan 內存在(外面為 None → 503)
    app.state.mcp_server = SpillwayServer(app)
    app.state.mcp_manager = None
    # CHG-20261102-01 T3(決策 13):list_changed 通知(停機掛鉤在 lifespan 內登記)
    app.state.mcp_events = McpEvents()
    app.state.db_path = path
    app.state.port = port   # CHG-20261102-01 T1(決策 4):Origin 比對用的 serve 綁定埠
    app.state.conn_factory = conn_factory
    install_error_handlers(app)

    app.include_router(openai_api.router)
    # ---- T15(CHG-20261030-02)只在此處追加下一行掛載管理端點,其餘不動 ----
    app.include_router(admin.router)
    # ---- T15 追加處結束 ----
    # CHG-20261102-01 T2(決策 9、10):`/mcp/<group>`(depth → Bearer → 停機 → 群組 → SDK session manager)
    app.mount("/mcp", McpEndpoint(app))
    # CHG-20261102-01 T1(決策 4、AM-11):Host/Origin 檢查。add_middleware 後加者在外層,
    # 所以這一行必須是 create_app 的最後一個 add_middleware,讓它先於所有路由與 `/mcp` 執行。
    app.add_middleware(HostOriginMiddleware, port=port)
    return app

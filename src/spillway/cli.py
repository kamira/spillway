"""命令列入口(CHG-20261030-02 T13;決策 3、4、21;design D16;CHG-20261101-03 T7 決策 5、14、15、17)。

`spillway serve [--host 127.0.0.1] [--port 8787]`、`spillway init [--rotate]`;`mcp-stdio` 於 M3b 加入。

serve:
- `--host` 只接受 `settings.ALLOWED_HOSTS`(字面 `127.0.0.1`;M3 仍只允許 127.0.0.1);其他值印錯誤到
  stderr 並回 2,不啟動。
- `--port` 必須為 1–65535。
- 啟動前置檢查 `_preflight(host, port, data_dir)`(決策 5 ①②):先以暫時 socket 試綁埠(隨即關閉),
  再以非阻塞方式取得資料目錄鎖 `spillway.lock` 並持有到行程結束;任一失敗印錯誤到 stderr、回
  `EXIT_STARTUP_FAILED`,不建 app、不開資料庫、不 migrate。
- 以 uvicorn 執行 `spillway.app.create_app(issue_token=True)`(`lifespan="on"`:lifespan 失敗即啟動
  失敗)。資料庫沒有存取 token 雜湊時,lifespan 產生 token 並以單行印到 stderr(只此一次)。
  退出碼:用法錯誤或 host 不允許回 2;啟動失敗(例如 7.6.6 模組衝突)回非 0——uvicorn 以
  `sys.exit(3)` 結束時沿用 3,未啟動而無退出碼時回 1(`EXIT_STARTUP_FAILED`)。
  Ctrl-C / SIGTERM:uvicorn 先完成正常關閉(lifespan shutdown、停止 RetentionJob 排程),再依 Unix
  慣例把收到的訊號重新送給自己,行程以該訊號結束(shell 看到 130 / 143,`subprocess` 看到 −2 / −15),
  不回 0;這讓上層的行程管理器分得出「被要求停止」與「自己結束」。本模組不改寫此行為。
- graceful shutdown(CHG-20261101-03 T8;決策 16、圖 4):第一個 SIGTERM/SIGINT 設
  `app.state.shutting_down = True`(Router 不再開始新嘗試,回 503 `shutting_down`),uvicorn 停止接新連線並
  等待進行中請求至多 `graceful_timeout` 秒(預設 30,傳給 `timeout_graceful_shutdown`),期滿取消請求
  task(CliRunner 在取消路徑殺 process group、帳本記 `error="shutdown"`);lifespan 結束時
  `terminate_all(grace=5)` 為後備。停機中再收到 SIGINT 為 uvicorn 的 `force_exit`(略過 lifespan):立即
  取消進行中請求(CliRunner SIGKILL 子行程 group),並在 uvicorn 重送訊號之前於 `capture_signals` 內同步
  `kill_all_sync()`;`_run_server` 的 `finally` 與 `atexit` 也呼叫它(force_exit 時帳本列可能不及寫入)。環境變數 `SPILLWAY_TEST_GRACEFUL_TIMEOUT`
  **僅供測試**縮短等待上限,正式使用不要設定。
- uvicorn access log 保留(只有方法、路徑、狀態碼,不含 body)。

init(決策 14、15):建立資料目錄與 `work/claude`、`work/codex`;在資料目錄鎖內開啟資料庫(執行
migration,含舊版庫的備份;不做埠檢查)並產生存取 token。token 明文只印到 stdout 一次(單獨一行),
資料庫只存 SHA-256;其他提示一律走 stderr(`TOKEN=$(spillway init)` 只會拿到 token 或空字串)。
已有 token 時在 stderr 印「已存在,未變更」並回 0;`--rotate` 才重新產生。鎖被占用(serve
正在執行)或 migration 失敗時回 `EXIT_STARTUP_FAILED`。

`main(argv)` 回傳退出碼;`python -m spillway` 以 `SystemExit(main())` 結束。
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import contextlib
import os
import socket
import sqlite3
import sys
from pathlib import Path
from collections.abc import Iterator
from types import FrameType
from typing import TYPE_CHECKING

from spillway import settings
from spillway.adapters.base import CLI_WORK_SUBDIRS, DEFAULT_DRAIN_TIMEOUT, DEFAULT_TERM_GRACE
if TYPE_CHECKING:   # uvicorn 延遲 import(init 不需要它)
    import uvicorn
    from fastapi import FastAPI

from spillway.store.db import (
    DataDirLockedError,
    MigrationError,
    SchemaVersionError,
    acquire_data_dir_lock,
    connect,
    data_dir_lock,
)

__all__ = ["main"]

EXIT_USAGE = 2
EXIT_STARTUP_FAILED = 1
WORK_SUBDIRS = CLI_WORK_SUBDIRS   # 決策 10、11、20(CHG-20261102-01):CLI 模組未設定 cwd 時的工作目錄
GRACEFUL_TIMEOUT = 30.0   # 決策 16 ②:停機時等待進行中請求的上限(秒)
TEST_GRACEFUL_TIMEOUT_ENV = "SPILLWAY_TEST_GRACEFUL_TIMEOUT"   # 僅供測試,見 _test_graceful_timeout
# 期滿取消後等被取消的請求走完 CliRunner 取消路徑的上限:SIGTERM 寬限 + 讀管線上限 + 1 秒
CANCELLED_DRAIN_TIMEOUT = DEFAULT_TERM_GRACE + DEFAULT_DRAIN_TIMEOUT + 1.0


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("埠號必須是整數") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("埠號必須在 1–65535")
    return port


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="spillway", description="單人使用的 LLM 路由閘道")
    sub = p.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="啟動 HTTP 伺服器")
    serve.add_argument("--host", default=settings.DEFAULT_HOST,
                       help=f"綁定位址;M3 仍只允許 {settings.DEFAULT_HOST}")
    serve.add_argument("--port", type=_port, default=settings.DEFAULT_PORT,
                       help=f"埠號(預設 {settings.DEFAULT_PORT})")
    init = sub.add_parser("init", help="建立資料目錄、升級資料庫並產生存取 token")
    init.add_argument("--rotate", action="store_true", help="重新產生存取 token(舊 token 失效)")
    return p


def _err(message: str) -> None:
    print(f"spillway: {message}", file=sys.stderr)


def _preflight(host: str, port: int, data_dir: Path) -> int:
    """serve 開啟資料庫前的前置檢查(決策 5 ①②);通過回 0,否則印錯誤並回非 0。

    ① 以暫時 socket 試綁 `(host, port)` 後關閉(與 uvicorn 一樣設 `SO_REUSEADDR`);
    ② 建立資料目錄並取得 `spillway.lock`(非阻塞),持有到行程結束。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
    except OSError as exc:
        _err(f"無法綁定 {host}:{port}({exc.strerror or type(exc).__name__});"
             "未開啟資料庫、未執行 migration")
        return EXIT_STARTUP_FAILED
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        acquire_data_dir_lock(data_dir)
    except DataDirLockedError as exc:
        _err(f"{exc};未執行 migration")
        return EXIT_STARTUP_FAILED
    except OSError as exc:
        _err(f"無法建立或鎖定資料目錄 {data_dir}({exc.strerror or type(exc).__name__})")
        return EXIT_STARTUP_FAILED
    return 0


def _graceful_server(config: uvicorn.Config, app: FastAPI) -> uvicorn.Server:
    """回傳 uvicorn.Server 子類別的實例(決策 16、圖 4)。

    - 第一個 SIGTERM/SIGINT:先設 `app.state.shutting_down = True`(Router 不再開始新嘗試),再交給
      uvicorn 原本的 `handle_exit`(保留第二次 SIGINT 的 `force_exit` 與結束後重送訊號的行為)。
    - 轉為 `force_exit` 的那一刻:在事件迴圈上立即取消全部進行中的請求 task——CliRunner 的取消路徑
      馬上 SIGKILL 子行程 group,連線結束,uvicorn 的 `wait_closed()` 不必等滿停機上限。
    - 期滿取消後、lifespan 結束前:有限時間等待被取消的請求走完取消路徑(帳本 `error="shutdown"`)。
    - `capture_signals` 結束(uvicorn 重送訊號)之前同步 `kill_all_sync()`:SIGTERM 後再 SIGINT
      (force_exit 略過 lifespan,且最後重送的 SIGTERM 是預設處置、行程立刻結束,`finally`/`atexit`
      都不會跑)也不留子行程。

    依賴的 uvicorn 內部行為(已對 uvicorn 0.54.0 `server.py` 核對;L2 子行程測試為絆線):
    `handle_exit` 設 `should_exit`/`force_exit`;`shutdown()` 期滿取消 `server_state.tasks` 但不等待,
    `force_exit` 時略過 `lifespan.shutdown()`;`self.lifespan` 於 `_serve` 建立、`shutdown()` 時呼叫其
    `shutdown` 方法;`capture_signals()` 在離開時還原處理器並以相反順序 `signal.raise_signal` 重送。"""
    import uvicorn

    runner = app.state.cli_runner

    class _GracefulServer(uvicorn.Server):
        _loop: asyncio.AbstractEventLoop | None = None

        async def startup(self, sockets: list[socket.socket] | None = None) -> None:
            self._loop = asyncio.get_running_loop()
            await super().startup(sockets)

        def handle_exit(self, sig: int, frame: FrameType | None) -> None:
            app.state.shutting_down = True
            was_forced = self.force_exit
            super().handle_exit(sig, frame)
            if self.force_exit and not was_forced and self._loop is not None:
                try:   # 訊號處理器內不直接取消;排到事件迴圈上(也會喚醒 selector)
                    self._loop.call_soon_threadsafe(self._cancel_inflight)
                except RuntimeError:   # 迴圈已關閉:capture_signals/finally/atexit 仍會殺子行程
                    pass

        def _cancel_inflight(self) -> None:
            for task in list(self.server_state.tasks):
                task.cancel(msg="force exit")

        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            with super().capture_signals():   # uvicorn 在它的離開處重送收到的訊號
                try:
                    yield
                finally:
                    runner.kill_all_sync()   # 在重送訊號之前;不需要事件迴圈

        async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
            # uvicorn 期滿取消請求 task 後**不等它們結束**就跑 lifespan 結束,且在 serve() 內重送訊號
            # (SIGTERM 為預設處置,行程立刻結束)——被取消的請求來不及走完 CliRunner 的取消路徑
            # (killpg SIGTERM→寬限→SIGKILL)與 Router 寫帳本 error="shutdown"。這裡在 lifespan 結束前
            # 有限時間等待那些 task(force_exit 會略過 lifespan,也就不等;等待中轉為 force_exit 時
            # `_cancel_inflight` 再次取消,CliRunner 立即 SIGKILL,task 很快結束)。
            lifespan_shutdown = self.lifespan.shutdown

            async def drain_then_lifespan_shutdown() -> None:
                pending = [t for t in self.server_state.tasks if not t.done()]
                if pending and not self.force_exit:
                    await asyncio.wait(pending, timeout=CANCELLED_DRAIN_TIMEOUT)
                await lifespan_shutdown()

            self.lifespan.shutdown = drain_then_lifespan_shutdown   # type: ignore[method-assign]
            await super().shutdown(sockets)

    return _GracefulServer(config)


def _test_graceful_timeout() -> float | None:
    """**僅供測試**:環境變數 `SPILLWAY_TEST_GRACEFUL_TIMEOUT`(秒)覆寫 serve 的停機等待上限,讓子行程
    測試不必等 30 秒。正式使用不要設定;值不合法(非數字或 ≤ 0)時忽略。"""
    raw = os.environ.get(TEST_GRACEFUL_TIMEOUT_ENV)
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _run_server(host: str, port: int, graceful_timeout: float = GRACEFUL_TIMEOUT) -> int:
    if rc := _preflight(host, port, settings.data_dir()):
        return rc

    import uvicorn

    from spillway import app as app_module

    app = app_module.create_app(issue_token=True)
    runner = app.state.cli_runner
    # 決策 16 ④:第二次 Ctrl-C(force_exit)會略過 lifespan;finally 與 atexit 是子行程清理的最後防線
    atexit.register(runner.kill_all_sync)
    config = uvicorn.Config(app, host=host, port=port, lifespan="on", log_level="info",
                            access_log=True, timeout_graceful_shutdown=graceful_timeout)
    server = _graceful_server(config, app)
    try:
        server.run()
    except SystemExit as exc:   # uvicorn 部分版本在啟動失敗時直接 sys.exit
        return exc.code if isinstance(exc.code, int) and exc.code != 0 else EXIT_STARTUP_FAILED
    finally:
        runner.kill_all_sync()
    return 0 if server.started else EXIT_STARTUP_FAILED


def _init(rotate: bool) -> int:
    """`spillway init [--rotate]`(決策 14、15)。"""
    home = settings.data_dir()
    try:
        for d in (home, *(home / "work" / name for name in WORK_SUBDIRS)):
            d.mkdir(parents=True, exist_ok=True)
        with data_dir_lock(home):
            conn = connect(settings.db_path())
            try:
                if rotate:
                    token: str | None = settings.rotate_access_token(conn)
                else:
                    token = settings.ensure_access_token(conn)
            finally:
                conn.close()
    except DataDirLockedError as exc:
        _err(f"{exc};init 未執行(要重新產生 token 可改用 POST /api/settings/token/rotate)")
        return EXIT_STARTUP_FAILED
    except (MigrationError, SchemaVersionError, sqlite3.Error, OSError) as exc:
        _err(f"init 失敗:{type(exc).__name__}: {exc}")
        return EXIT_STARTUP_FAILED
    if token is None:
        _err("存取 token 已存在,未變更(要重新產生請執行 spillway init --rotate)")
        return 0
    _err(f"資料目錄 {home};已產生存取 token(印在 stdout,只顯示這一次;資料庫只存 SHA-256 雜湊)")
    print(token, flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:   # argparse 錯誤或 --help
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    if args.command == "init":
        return _init(args.rotate)
    if args.command != "serve":
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    if not settings.is_allowed_host(args.host):
        allowed = ", ".join(sorted(settings.ALLOWED_HOSTS))
        _err(f"拒絕綁定 {args.host!r}:M3 仍只允許 {allowed}")
        return EXIT_USAGE
    if (graceful := _test_graceful_timeout()) is not None:
        return _run_server(args.host, args.port, graceful_timeout=graceful)
    return _run_server(args.host, args.port)

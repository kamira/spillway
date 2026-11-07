"""命令列入口(CHG-20261030-02 T13;決策 3、4、21;design D16;CHG-20261101-03 T7 決策 5、14、15、17)。

`spillway serve [--host 127.0.0.1] [--port 8787]`、`spillway init [--rotate]`、
`spillway mcp-stdio --group <g> [--port 8787]`(CHG-20261102-01 T4;見 `spillway.api.mcp.bridge`)。

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
  慣例把收到的訊號重新送給自己,不回 0;這讓上層的行程管理器分得出「被要求停止」與「自己結束」。
  SIGTERM:行程以該訊號結束(shell 看到 143,`subprocess` 看到 −15)。Ctrl-C(SIGINT,CHG-20261102-01
  決策 19):重送的 SIGINT 在 `main` 呼叫 `_run_server` 的外層以 `KeyboardInterrupt` 捕捉,不印 traceback,
  **結束碼 130**(`EXIT_SIGINT`;shell 與 `subprocess` 都看到 130)。
- graceful shutdown(CHG-20261101-03 T8;決策 16、圖 4):第一個 SIGTERM/SIGINT 設
  `app.state.shutting_down = True`(Router 不再開始新嘗試,回 503 `shutting_down`),uvicorn 停止接新連線並
  等待進行中請求至多 `graceful_timeout` 秒(預設 30,傳給 `timeout_graceful_shutdown`),期滿取消請求
  task(CliRunner 在取消路徑殺 process group、帳本記 `error="shutdown"`);lifespan 結束時
  `terminate_all(grace=5)` 為後備。停機中再收到 SIGINT 為 uvicorn 的 `force_exit`(略過 lifespan):立即
  取消進行中請求(CliRunner SIGKILL 子行程 group),並在 uvicorn 重送訊號之前於 `capture_signals` 內同步
  `kill_all_sync()`;`_run_server` 的 `finally` 與 `atexit` 也呼叫它(force_exit 時帳本列可能不及寫入)。環境變數 `SPILLWAY_TEST_GRACEFUL_TIMEOUT`
  **僅供測試**縮短等待上限,正式使用不要設定。
- 停機期滿與 MCP(CHG-20261102-01 決策 14、18):`_run_server` 在 `server.run()` 之前呼叫
  `sse_starlette` 的 `AppStatus.disable_automatic_graceful_drain()`(不設 `AppStatus.should_exit`)。第一個
  訊號時以 `call_soon_threadsafe` 觸發 `app.state.shutdown_hooks.fire`,並排定
  `call_later(max(grace*0.5, grace-1.5), app.state.drain_expired.set)`:經 `api.drain.run_until_drain` 的
  進行中請求在 uvicorn 期滿取消之前被取消(CliRunner 殺 group、帳本 `error="shutdown"`)並回 503
  `shutting_down`。被取消但尚未收尾的 `drain_tasks` 與期滿取消的請求一起在 lifespan 結束前有限等待;
  force_exit 的 `_cancel_inflight` 也取消 `app.state.drain_tasks`。寬限小於約 1.2 秒(僅測試用範圍)時,
  uvicorn 的期滿取消可能早於 drain 收尾。
- uvicorn access log 保留(只有方法、路徑、狀態碼,不含 body)。
- 測試控制介面(CHG-20261107-03 決策 5、6;spec「測試控制介面(僅測試模式)」「行程契約」):**只有**
  `_run_server` 讀 `SPILLWAY_TEST_CONTROL` 與啟動期旋鈕(`TEST_KNOB_ENVS`),再以明確參數傳給
  `create_app(test_control=True, start_time=…, rng_seed=…, extra_adapter_paths=…,
  retention_poll_seconds=…, retention_stepped=…, issue_token=…)`。
  - `SPILLWAY_TEST_CONTROL=1`(只認字面 `1`)時 `SPILLWAY_HOME` 必須明確設定,且解析後不等於
    `platformdirs.user_data_dir("spillway")`;否則印錯誤、回 `EXIT_USAGE`(2),不建資料目錄、不開資料庫。
    旋鈕的值不合法時同樣回 2(訊息只含變數名,不含值)。通過後在 stderr 印一行 `TEST_CONTROL_WARNING`。
  - 未開啟時旋鈕一律不讀;有設定的旋鈕在 stderr 印一行 `TEST_KNOBS_IGNORED` + 名稱(以「, 」相接),不印值。
    只比對 `TEST_KNOB_ENVS` 列舉的名稱(`SPILLWAY_TEST_*` 不是保留前綴);`SPILLWAY_TEST_GRACEFUL_TIMEOUT`、
    `SPILLWAY_TEST_MCP_MAX_SESSIONS` 是有意的例外,不受 CONTROL 限制、行為不變。
  - `SPILLWAY_TEST_LOG_LEVEL`(`DEBUG`/`INFO`):root logger 掛一個 stderr StreamHandler(等級即該值,root
    等級同步設定),每筆紀錄 `SPILLWAY-LOG <levelname> <name> <message>` 一行(args 已代入),多行內容
    (含 exc_info 與 stack)以 `SPILLWAY-LOG-CONT ` 前綴續行;不加遮罩 filter。在 uvicorn 設定 logging 之後安裝。

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
import logging
import math
import os
import re
import socket
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from collections.abc import Iterator, Mapping
from types import FrameType
from typing import TYPE_CHECKING

import platformdirs

from spillway import settings
from spillway.adapters.base import CLI_WORK_SUBDIRS
from spillway.adapters.base.cli_runner import TEST_CONTROL_ENV_NAMES
from spillway.api.test_control import parse_time

try:   # POSIX;Windows 沒有 pwd,只比對目前環境下的預設目錄
    import pwd
except ImportError:   # pragma: no cover
    pwd = None   # type: ignore[assignment]
# 期滿取消後等被取消的請求走完 CliRunner 取消路徑的上限(單一定義在 api/drain.py;模組屬性保留)
from spillway.api.drain import CANCELLED_DRAIN_TIMEOUT
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
EXIT_SIGINT = settings.EXIT_SIGINT   # CHG-20261102-01 決策 19:serve 被 Ctrl-C 結束(單一定義在 settings)
WORK_SUBDIRS = CLI_WORK_SUBDIRS   # 決策 10、11、20(CHG-20261102-01):CLI 模組未設定 cwd 時的工作目錄
GRACEFUL_TIMEOUT = 30.0   # 決策 16 ②:停機時等待進行中請求的上限(秒)
TEST_GRACEFUL_TIMEOUT_ENV = "SPILLWAY_TEST_GRACEFUL_TIMEOUT"   # 僅供測試,見 _test_graceful_timeout
TEST_MCP_MAX_SESSIONS_ENV = "SPILLWAY_TEST_MCP_MAX_SESSIONS"   # 僅供測試,見 _test_mcp_max_sessions

# CHG-20261107-03 決策 5、6:測試控制介面開關與啟動期旋鈕(名稱單一定義在 CliRunner,子行程一律移除)
TEST_CONTROL_ENV = TEST_CONTROL_ENV_NAMES[0]          # "SPILLWAY_TEST_CONTROL"
TEST_KNOB_ENVS = TEST_CONTROL_ENV_NAMES[1:]
(TEST_START_TIME_ENV, TEST_RNG_SEED_ENV, TEST_ADAPTER_PATHS_ENV, TEST_RETENTION_POLL_ENV,
 TEST_RETENTION_STEPPED_ENV, TEST_NO_TOKEN_ENV, TEST_LOG_LEVEL_ENV) = TEST_KNOB_ENVS
TEST_LOG_LEVELS = ("DEBUG", "INFO")
LOG_PREFIX = "SPILLWAY-LOG"
LOG_CONT_PREFIX = "SPILLWAY-LOG-CONT"
TEST_CONTROL_WARNING = ("spillway: 警告:測試控制介面已開啟(SPILLWAY_TEST_CONTROL=1),"
                        "/__test__/* 可改寫狀態;僅供測試,正式使用請勿設定")
TEST_KNOBS_IGNORED = "spillway: 未設 SPILLWAY_TEST_CONTROL=1,已忽略測試旋鈕: "
_SEED_RE = re.compile(r"-?[0-9]+")                 # 施工審議 F4:ASCII 十進位整數,i64 範圍
_POLL_RE = re.compile(r"[0-9]+(\.[0-9]+)?")        # 施工審議 F4:ASCII 十進位,不接受指數、底線、inf
_I64_MIN, _I64_MAX = -(2**63), 2**63 - 1


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
    # mcp-stdio(CHG-20261102-01 T4;決策 15):stdio ↔ http://127.0.0.1:<port>/mcp/<group>;不提供 --host/--url
    stdio = sub.add_parser("mcp-stdio", help="以 stdio 轉送 MCP 到本機閘道(token 取自環境變數 SPILLWAY_TOKEN)")
    stdio.add_argument("--group", required=True, help="MCP 群組名(all 表示全部 route)")
    stdio.add_argument("--port", type=_port, default=settings.DEFAULT_PORT,
                       help=f"閘道埠號(預設 {settings.DEFAULT_PORT};主機固定 127.0.0.1)")
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
            was_exiting, was_forced = self.should_exit, self.force_exit
            super().handle_exit(sig, frame)
            if self._loop is None:
                return
            try:   # 訊號處理器內不直接動事件迴圈;一律排到迴圈上(也會喚醒 selector)
                if not was_exiting:   # 第一個訊號(CHG-20261102-01 決策 14 ②、18)
                    self._loop.call_soon_threadsafe(app.state.shutdown_hooks.fire)
                    self._loop.call_soon_threadsafe(self._schedule_drain_expired)
                if self.force_exit and not was_forced:
                    self._loop.call_soon_threadsafe(self._cancel_inflight)
            except RuntimeError:   # 迴圈已關閉:capture_signals/finally/atexit 仍會殺子行程
                pass

        def _schedule_drain_expired(self) -> None:
            # 決策 18:在 uvicorn 期滿取消之前設定 drain_expired,讓 run_until_drain 先取消子 task、回 503
            grace = self.config.timeout_graceful_shutdown
            if grace is None or self._loop is None:
                return
            # 寬限 < 約 1.2 秒(僅測試用)時 uvicorn 的期滿取消可能早於 drain 收尾
            self._loop.call_later(drain_expire_delay(grace), app.state.drain_expired.set)

        def _cancel_inflight(self) -> None:
            for task in list(self.server_state.tasks):
                task.cancel(msg="force exit")
            for task in list(app.state.drain_tasks):   # MCP 工具等不在 server_state.tasks 的 task
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
                # 期滿取消的請求 task + run_until_drain 留下、仍在收尾的子 task(決策 18)
                pending = {t for t in (*self.server_state.tasks, *app.state.drain_tasks) if not t.done()}
                if pending and not self.force_exit:
                    await asyncio.wait(pending, timeout=CANCELLED_DRAIN_TIMEOUT)
                await lifespan_shutdown()

            self.lifespan.shutdown = drain_then_lifespan_shutdown   # type: ignore[method-assign]
            await super().shutdown(sockets)

    return _GracefulServer(config)


def drain_expire_delay(grace: float) -> float:
    """第一個訊號後多久設定 `app.state.drain_expired`(決策 18):`max(grace*0.5, grace-1.5)` 秒。"""
    return max(grace * 0.5, grace - 1.5)


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


def _test_mcp_max_sessions() -> int | None:
    """**僅供測試**(CHG-20261102-02 決策 19):環境變數 `SPILLWAY_TEST_MCP_MAX_SESSIONS` 覆寫 serve 的 MCP
    session 上限(預設 `api.mcp.server.MAX_SESSIONS`),讓子行程測試不必開 100 個 session。正式使用不要設定;
    值不合法(非整數或 ≤ 0)時忽略。"""
    raw = os.environ.get(TEST_MCP_MAX_SESSIONS_ENV)
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


@dataclass(frozen=True)
class _TestControl:
    """`SPILLWAY_TEST_CONTROL=1` 時解析出的旋鈕(CHG-20261107-03 決策 6)。"""

    start_time: datetime | None = None
    rng_seed: int | None = None
    adapter_paths: tuple[str, ...] = ()
    retention_poll_seconds: float | None = None
    retention_stepped: bool = False
    no_token: bool = False
    log_level: str | None = None


class _KnobError(ValueError):
    """旋鈕的值不合法;訊息只含變數名與原因,不含值。"""


def _flag(env: Mapping[str, str], name: str) -> bool:
    raw = env.get(name, "")
    if raw == "":
        return False
    if raw != "1":
        raise _KnobError(f"{name} 只接受 1")
    return True


def _read_test_control(env: Mapping[str, str]) -> _TestControl | None:
    """讀測試控制開關與旋鈕(決策 5 ①、6)。未開啟時回 None,並在 stderr 只印有設定的旋鈕名稱;
    開啟時回 `_TestControl`;值不合法丟 `_KnobError`。"""
    if env.get(TEST_CONTROL_ENV) != "1":
        ignored = [name for name in TEST_KNOB_ENVS if env.get(name)]   # 空字串等同未設定(F7)
        if ignored:
            print(TEST_KNOBS_IGNORED + ", ".join(ignored), file=sys.stderr, flush=True)
        return None
    start_time = rng_seed = poll = level = None
    if raw := env.get(TEST_START_TIME_ENV, ""):
        try:
            start_time = parse_time(raw)   # 與控制端點同一文法(F4)
        except ValueError:
            raise _KnobError(f"{TEST_START_TIME_ENV} 必須是 YYYY-MM-DDTHH:MM:SSZ") from None
    if raw := env.get(TEST_RNG_SEED_ENV, ""):
        if not _SEED_RE.fullmatch(raw) or not _I64_MIN <= int(raw) <= _I64_MAX:
            raise _KnobError(f"{TEST_RNG_SEED_ENV} 必須是 -2^63 到 2^63-1 的十進位整數")
        rng_seed = int(raw)
    if raw := env.get(TEST_RETENTION_POLL_ENV, ""):
        poll = float(raw) if _POLL_RE.fullmatch(raw) else math.nan
        if not math.isfinite(poll) or poll <= 0:
            raise _KnobError(f"{TEST_RETENTION_POLL_ENV} 必須是正的十進位數(秒)")
    if raw := env.get(TEST_LOG_LEVEL_ENV, ""):
        if raw not in TEST_LOG_LEVELS:
            raise _KnobError(f"{TEST_LOG_LEVEL_ENV} 只接受 {'、'.join(TEST_LOG_LEVELS)}")
        level = raw
    paths = tuple(p for p in env.get(TEST_ADAPTER_PATHS_ENV, "").split(os.pathsep) if p)
    return _TestControl(start_time=start_time, rng_seed=rng_seed, adapter_paths=paths,
                        retention_poll_seconds=poll,
                        retention_stepped=_flag(env, TEST_RETENTION_STEPPED_ENV),
                        no_token=_flag(env, TEST_NO_TOKEN_ENV), log_level=level)


_CASE_INSENSITIVE_FS = sys.platform in ("darwin", "win32")


def _default_data_dirs() -> list[Path]:
    """施工審議 F1:可能是擁有者預設資料目錄的位置。(a) 目前環境下的 `platformdirs.user_data_dir`;
    (b) 把 (a) 相對於 `Path.home()` 的部分接到帳號家目錄(`pwd` 的 `pw_dir`)底下——`$HOME` 被覆寫
    時仍找得到真正的預設目錄;(c) Linux 另加 `pw_dir/.local/share/spillway`(非 XDG 的預設位置)。"""
    current = Path(platformdirs.user_data_dir("spillway"))
    dirs = [current]
    if pwd is not None:
        pw_dir = Path(pwd.getpwuid(os.getuid()).pw_dir)
        try:
            dirs.append(pw_dir / current.relative_to(Path.home()))
        except ValueError:   # (a) 不在 $HOME 底下(例如 XDG_DATA_HOME 指到別處)
            pass
        if sys.platform.startswith("linux"):
            dirs.append(pw_dir / ".local" / "share" / "spillway")
    return dirs


def _same_dir(a: Path, b: Path) -> bool:
    """同一目錄:兩者都存在且 `samefile`,或 `resolve()` 後字串相等(macOS、Windows 先 casefold)。"""
    try:
        if a.exists() and b.exists() and os.path.samefile(a, b):
            return True
    except OSError:
        pass
    sa, sb = str(a.resolve()), str(b.resolve())
    if _CASE_INSENSITIVE_FS:
        sa, sb = sa.casefold(), sb.casefold()
    return sa == sb


def _test_home_problem(env: Mapping[str, str]) -> str | None:
    """決策 5 ②:測試模式的資料目錄必須明確設定,且不與預設(擁有者)資料目錄是同一個目錄(含大小寫
    變體、符號連結與以帳號家目錄推得的位置,F1);符合時回 None。不讀寫 `os.environ`。"""
    raw = env.get(settings.ENV_HOME, "")
    if not raw:
        return f"{TEST_CONTROL_ENV}=1 時必須明確設定 {settings.ENV_HOME}(測試模式不使用預設資料目錄)"
    home = Path(raw)
    if any(_same_dir(home, d) for d in _default_data_dirs()):
        return f"{TEST_CONTROL_ENV}=1 時 {settings.ENV_HOME} 不可為預設資料目錄"
    return None


class _TestLogFormatter(logging.Formatter):
    """`SPILLWAY-LOG <levelname> <name> <message>`;多行內容以 `SPILLWAY-LOG-CONT ` 續行(決策 6)。"""

    def format(self, record: logging.LogRecord) -> str:
        text = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            text = f"{text}\n{record.exc_text}"
        if record.stack_info:
            text = f"{text}\n{self.formatStack(record.stack_info)}"
        first, *rest = text.splitlines() or [""]
        lines = [f"{LOG_PREFIX} {record.levelname} {record.name} {first}"]
        lines.extend(f"{LOG_CONT_PREFIX} {line}" for line in rest)
        return "\n".join(lines)


def _install_test_log_handler(level: str) -> logging.Handler:
    """root logger 掛 stderr handler(等級即 `level`,root 等級同步設定);回傳 handler。"""
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(_TestLogFormatter())
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    return handler


def _run_server(host: str, port: int, graceful_timeout: float = GRACEFUL_TIMEOUT) -> int:
    # CHG-20261107-03 決策 5 ①②:測試控制只在這裡讀環境變數;HOME 檢查在建資料目錄與開資料庫之前
    try:
        control = _read_test_control(os.environ)
    except _KnobError as exc:
        _err(f"{exc};未啟動")
        return EXIT_USAGE
    if control is not None:
        if problem := _test_home_problem(os.environ):
            _err(f"{problem};未建立資料目錄、未開啟資料庫")
            return EXIT_USAGE
        print(TEST_CONTROL_WARNING, file=sys.stderr, flush=True)
    if rc := _preflight(host, port, settings.data_dir()):
        return rc

    import uvicorn

    from spillway import app as app_module

    mcp_kwargs = {} if (n := _test_mcp_max_sessions()) is None else {"mcp_max_sessions": n}
    control_kwargs: dict[str, object] = {}
    if control is not None:
        control_kwargs = {"test_control": True, "start_time": control.start_time,
                          "rng_seed": control.rng_seed, "extra_adapter_paths": control.adapter_paths,
                          "retention_stepped": control.retention_stepped}
        if control.retention_poll_seconds is not None:
            control_kwargs["retention_poll_seconds"] = control.retention_poll_seconds
    issue_token = control is None or not control.no_token
    app = app_module.create_app(issue_token=issue_token, port=port, **mcp_kwargs, **control_kwargs)
    runner = app.state.cli_runner
    # 決策 16 ④:第二次 Ctrl-C(force_exit)會略過 lifespan;finally 與 atexit 是子行程清理的最後防線
    atexit.register(runner.kill_all_sync)
    config = uvicorn.Config(app, host=host, port=port, lifespan="on", log_level="info",
                            access_log=True, timeout_graceful_shutdown=graceful_timeout)
    if control is not None and control.log_level is not None:
        _install_test_log_handler(control.log_level)   # uvicorn.Config 已設定 logging,之後才掛
    server = _graceful_server(config, app)
    # 決策 14 ①:只關 GET 串流由 shutdown_hooks 負責;sse-starlette 的 auto-drain 會關掉所有 SSE(含 POST 回應)
    from sse_starlette.sse import AppStatus   # pyproject 明列 sse-starlette>=3.5.0(CHG-20261102-01 T2)
    AppStatus.disable_automatic_graceful_drain()
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
    if args.command == "mcp-stdio":   # 結束碼 0/1/2/3/130 見 api/mcp/bridge.py;不開資料庫、不取鎖
        from spillway.api.mcp.bridge import run as run_mcp_stdio
        return run_mcp_stdio(args.group, args.port)
    if args.command != "serve":
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    if not settings.is_allowed_host(args.host):
        allowed = ", ".join(sorted(settings.ALLOWED_HOSTS))
        _err(f"拒絕綁定 {args.host!r}:M3 仍只允許 {allowed}")
        return EXIT_USAGE
    try:   # 決策 19:Ctrl-C 時 uvicorn 重發 SIGINT → KeyboardInterrupt;不印 traceback,結束碼 130
        if (graceful := _test_graceful_timeout()) is not None:
            return _run_server(args.host, args.port, graceful_timeout=graceful)
        return _run_server(args.host, args.port)
    except KeyboardInterrupt:
        return EXIT_SIGINT

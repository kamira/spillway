"""黑箱 harness:以子行程操作 spillway(CHG-20261107-03 T2;決策 8;AM-6;spec 13、14)。

黑箱測試只經 HTTP、子行程與唯讀資料庫操作 spillway,不 import `spillway`(邊界守門見
`tests/contract/test_blackbox_boundary.py`)。換實作時只要設定 `SPILLWAY_CMD`。

命令
- `SPILLWAY_CMD`:以 `shlex.split` 切開的程式前綴,預設 `[sys.executable, "-m", "spillway"]`;
  `serve`、`init`、`mcp-stdio` 一律經它執行(`spillway_cmd()`)。
- `child_env(home, *, fake_bin=None, extra=None)`:子行程環境——複製目前環境、移除真實 API key、
  `*_proxy`、`SPILLWAY_TOKEN`、`SPILLWAY_DEPTH`、`SPILLWAY_CMD` 與全部測試旋鈕(由呼叫端明確給),
  `SPILLWAY_HOME` 設為 `home`(必須在暫存目錄下),`PYTHONPATH` 的相對項目轉成絕對路徑;PATH 經
  `safe_subprocess_env` 清理並斷言只找得到假 `claude`/`codex`(`fake_bin` 給了就放最前面)。
- `run_cli(*args, home, fake_bin=None, extra_env=None, input=None, timeout=60)`:執行
  `SPILLWAY_CMD <args>`(例如 `init`、`init --rotate`)並回傳 `CompletedProcess`(text)。

`Serve(home, **opts)`:一個 `serve` 子行程(`start()` 或 `with Serve(...) as s:`)。
- 選項:`control=True`(`SPILLWAY_TEST_CONTROL=1`)、`start_time`、`rng_seed`、`adapter_paths`、
  `retention_poll_seconds`、`retention_stepped`、`no_token`、`log_level`(spec 13.2 旋鈕;`control=False`
  時不設定任何旋鈕)、`graceful_timeout=3.0`(`SPILLWAY_TEST_GRACEFUL_TIMEOUT`;None 為不設定)、
  `mcp_max_sessions`、`extra_env`、`fake_bin`、`port`(預設隨機空埠)、`log_path`、
  `allowed_unavailable_adapters`(允許載入失敗的模組名)、`allow_stderr`(stop() 時允許的問題行 regex)。
- 啟動:行程群組獨立(`start_new_session=True`),stdout 丟棄,stderr 由背景執行緒讀取:原始位元組留在
  記憶體(`stderr_bytes()`),寫入 `log_path` 的副本把 token 換成 `***`。
- 就緒(spec 14.4):`GET /api/settings/context` 回 200 且行程仍在執行;不看 pid、lsof。
- token 只從 spec 14.2 的 `TOKEN_NOTICE` 單行解析(`token`;未簽發時 None)。
- `adapter_paths` 非空時,就緒後以 `GET /api/adapters` 斷言每個路徑下的每個模組資料夾(含 `__init__.py`
  的子目錄,spec 7.6.1)都已被列出(`builtin` 為 false),且除 `allowed_unavailable_adapters` 外皆可用;
  路徑下沒有任何模組資料夾也算失敗(不存在的路徑會被 serve 靜默略過,T1 審議 A5)。
- `restart(extra_env=None, adapter_paths=None, **opts)`:停機前讀 `GET /__test__/clock`,可控時鐘時以該時刻
  作為新的 `START_TIME`;其餘旋鈕(含 `RNG_SEED`,即重新播種)沿用,`opts` 覆寫;同一資料目錄、同一埠。
  啟動後要改環境的步驟一律經它重啟。
- `stop(sig=SIGTERM, *, timeout=None, expect_code=None, allow_stderr=())`:送訊號到行程群組並等待結束,
  斷言:結束碼符合 spec 14.1(SIGTERM → `-15`、SIGINT → `130`;行程已先結束時比對 `expect_code`);
  stderr 沒有 `Traceback`、沒有 ERROR/CRITICAL 行(`stderr_problems`;`allow_stderr` 與建構時的
  `allow_stderr` 為例外);未在 `timeout`(預設停機上限 + 20 秒)內結束時 SIGKILL 並失敗。冪等。
- `kill()`:不做任何斷言,SIGKILL 行程群組(測試已失敗時的收尾)。
- `signal(sig)`、`wait_exit(timeout) -> 經過秒數`、`returncode`。
- 位址與 HTTP:`port`、`base_url`、`url(path)`、`mcp_url(group)`、`auth_headers(token=None)`、
  `http(**kw)`(同步 `httpx.Client`,`trust_env=False`)、`async_http(**kw)`、`request(method, path, **kw)`。
- `control`:`tests.blackbox.control.Control`(只在 `control=True` 時可用)。
- 資料庫:`db_path`、`db()`(唯讀連線,`tests.blackbox.db.read_db`)。
- `rotate_token()`:`POST /api/settings/token/rotate`,更新並回傳 `token`。
- MCP:`mcp_session(group="all", *, token=<s.token>, headers=None, message_handler=None, read_timeout=60)`
  (async context manager,yield `McpClient`);`stream_open(session_id)`;
  `await wait_stream_open(session_id, timeout=5.0)`。
- stderr:`stderr_bytes()`、`stderr_text()`(token 已換成 `***`)、`stderr_offset()`、
  `stderr_since(offset) -> bytes`、`drain_stderr(quiet=0.3, timeout=5.0)`(等到一段時間沒有新輸出)。

訊號送到整個行程群組(AM-6)。`SPILLWAY_CMD` 指向的程式必須擇一(施工審議 T2 F4):① 以 exec 取代自己
成為實作;② 讓實作另開行程群組、轉送 SIGINT/SIGTERM,在實作結束後以相同結束狀態結束——實作以訊號結束時,
重設該訊號為預設處置後對自己重送同一訊號(不可改成 `exit 128+n`)。包裝程式與實作若在同一個群組,包裝程式
不可再轉送訊號(否則實作會收到兩次;第二個 SIGINT 為強制結束)。停機後包裝程式與實作的所有子孫行程不得殘留,
也不得繼續持有 stderr:`stop()` 在行程結束後斷言 stderr 已到 EOF、行程群組已清空(否則 SIGKILL 該群組並失敗)。
`SPILLWAY_CMD` 的第一個字是含路徑分隔字元的相對路徑(例如 `./wrap.sh`、`target/debug/spillway`)時,
以測試行程啟動時的工作目錄解析(`INVOCATION_DIR`;子行程的 cwd 是暫存目錄)。

就緒:開啟控制介面時,先在 stderr 看到 14.3.3 的警告行(spec 規定在開始監聽前寫出)才開始輪詢
`/api/settings/context`,避免連到恰好佔用同一埠的其他伺服器。存活中的 `Serve` 登記在模組層的 `_LIVE`,
行程結束時(atexit)對其行程群組 SIGKILL;測試行程本身被 SIGKILL 時無法處理(已知限制)。

stderr 問題掃描(`stderr_problems`):`SPILLWAY-LOG` 行以外的字樣(Python 的 `Traceback`、uvicorn 的
`ERROR:`、Rust 的 `panicked at`)是盡力而為,不是契約(spec 14.3.1)。

常數(spec 14):`TOKEN_NOTICE`、`TEST_CONTROL_WARNING`、`KNOBS_IGNORED_PREFIX`、`CONTROL_ENV`、
`KNOB_ENVS`(依 spec 13.2 表格順序)、`GRACEFUL_ENV`、`MCP_MAX_SESSIONS_ENV`、`EXIT_USAGE`(2)、
`EXIT_PREFLIGHT`(1)、`EXIT_STARTUP`(3)、`EXIT_SIGINT`(130)、`EXIT_SIGTERM`(-15,`subprocess` 的值)。
`drain_expire_delay(grace)`:第一個停機訊號後多久把進行中的 MCP/CLI 請求取消並回 503
(`max(grace*0.5, grace-1.5)` 秒;CHG-20261102-01 決策 18,spec 14.1「正常關閉」;Python 合約測試
`tests/contract/test_schema_python.py` 核對實作)。
"""
from __future__ import annotations

import atexit
import contextlib
import os
import re
import shlex
import signal as _signal
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from tests.blackbox.control import Control, iso
from tests.blackbox.db import db_path as _db_path
from tests.blackbox.db import read_db
from tests.blackbox.mask import redact_tokens
from tests.blackbox.ports import free_port
from tests.fakes.cli_upstream import assert_only_fakes, safe_subprocess_env

__all__ = [
    "CMD_ENV", "CONTROL_ENV", "EXIT_PREFLIGHT", "EXIT_SIGINT", "EXIT_SIGTERM", "EXIT_STARTUP", "EXIT_USAGE",
    "GRACEFUL_ENV", "KNOBS_IGNORED_PREFIX", "KNOB_ENVS", "MCP_MAX_SESSIONS_ENV", "REAL_KEY_VARS",
    "TEST_CONTROL_WARNING", "TOKEN_NOTICE", "Serve", "assert_only_fakes", "child_env", "drain_expire_delay",
    "parse_tokens", "run_cli", "spillway_cmd", "stderr_problems",
]

CMD_ENV = "SPILLWAY_CMD"
HOST = "127.0.0.1"
REAL_KEY_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# spec 14.2.1(前綴位元組見 spec;tests/blackbox/test_process_contract.py 逐位元組核對)
TOKEN_NOTICE = "spillway: 已產生存取 token(只顯示這一次,資料庫只存雜湊): "
TOKEN_RE = re.compile(rb"[A-Za-z0-9_-]{43}")
# spec 14.3.3、14.3.4
TEST_CONTROL_WARNING = ("spillway: 警告:測試控制介面已開啟(SPILLWAY_TEST_CONTROL=1),"
                        "/__test__/* 可改寫狀態;僅供測試,正式使用請勿設定")
KNOBS_IGNORED_PREFIX = "spillway: 未設 SPILLWAY_TEST_CONTROL=1,已忽略測試旋鈕: "
LOG_PREFIX = "SPILLWAY-LOG "
LOG_CONT_PREFIX = "SPILLWAY-LOG-CONT "

# spec 13.1、13.2(表格順序)、13.2.2
CONTROL_ENV = "SPILLWAY_TEST_CONTROL"
START_TIME_ENV = "SPILLWAY_TEST_START_TIME"
RNG_SEED_ENV = "SPILLWAY_TEST_RNG_SEED"
ADAPTER_PATHS_ENV = "SPILLWAY_TEST_ADAPTER_PATHS"
RETENTION_POLL_ENV = "SPILLWAY_TEST_RETENTION_POLL_SECONDS"
RETENTION_STEPPED_ENV = "SPILLWAY_TEST_RETENTION_STEPPED"
NO_TOKEN_ENV = "SPILLWAY_TEST_NO_TOKEN"
LOG_LEVEL_ENV = "SPILLWAY_TEST_LOG_LEVEL"
KNOB_ENVS = (START_TIME_ENV, RNG_SEED_ENV, ADAPTER_PATHS_ENV, RETENTION_POLL_ENV, RETENTION_STEPPED_ENV,
             NO_TOKEN_ENV, LOG_LEVEL_ENV)
GRACEFUL_ENV = "SPILLWAY_TEST_GRACEFUL_TIMEOUT"
MCP_MAX_SESSIONS_ENV = "SPILLWAY_TEST_MCP_MAX_SESSIONS"

# spec 14.1
EXIT_PREFLIGHT = 1
EXIT_USAGE = 2
EXIT_STARTUP = 3
EXIT_SIGINT = 130
EXIT_SIGTERM = -_signal.SIGTERM

_DROP_ENV = frozenset({*REAL_KEY_VARS, "SPILLWAY_TOKEN", "SPILLWAY_DEPTH", CMD_ENV, CONTROL_ENV, *KNOB_ENVS,
                       GRACEFUL_ENV, MCP_MAX_SESSIONS_ENV})
_PROBLEM_RE = re.compile(r"Traceback|^ERROR\b|^CRITICAL\b|^SPILLWAY-LOG (ERROR|CRITICAL) |panicked at|"
                         r"^thread '.*' panicked")
# 測試行程啟動時的工作目錄(相對路徑的 SPILLWAY_CMD 以它解析;F5)
INVOCATION_DIR = Path(os.getcwd())
_DEFAULT = object()


def drain_expire_delay(grace: float) -> float:
    return max(grace * 0.5, grace - 1.5)


def spillway_cmd() -> list[str]:
    raw = os.environ.get(CMD_ENV, "")
    if not raw.strip():
        return [sys.executable, "-m", "spillway"]
    cmd = shlex.split(raw)
    first = cmd[0]
    if (os.sep in first or (os.altsep and os.altsep in first)) and not os.path.isabs(first):
        cmd[0] = str((INVOCATION_DIR / first).resolve())
    return cmd


def _require_tmp(home: Path) -> Path:
    home = Path(home).resolve()
    if not home.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise RuntimeError(f"SPILLWAY_HOME 必須在暫存目錄下:{home}")
    return home


def _abs_pythonpath(value: str) -> str:
    return os.pathsep.join(str(Path(p).resolve()) if p else p for p in value.split(os.pathsep))


def child_env(home: Path | str, *, fake_bin: Path | None = None,
              extra: Mapping[str, str] | None = None) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if k not in _DROP_ENV and not k.lower().endswith("_proxy")}
    base["SPILLWAY_HOME"] = str(_require_tmp(Path(home)))
    base["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    if base.get("PYTHONPATH"):
        base["PYTHONPATH"] = _abs_pythonpath(base["PYTHONPATH"])
    base.update(extra or {})
    return safe_subprocess_env(base, fake_bin=fake_bin)


def run_cli(*args: str, home: Path | str, fake_bin: Path | None = None,
            extra_env: Mapping[str, str] | None = None, input: str | None = None,
            timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    home = _require_tmp(Path(home))
    return subprocess.run([*spillway_cmd(), *args], env=child_env(home, fake_bin=fake_bin, extra=extra_env),
                          cwd=str(home.parent), input=input, capture_output=True, text=True, timeout=timeout)


def parse_tokens(stderr: bytes) -> list[str]:
    """stderr 中每一行 `TOKEN_NOTICE` + 43 字元 token 的 token(spec 14.2.1;只認整行)。"""
    prefix = TOKEN_NOTICE.encode("utf-8")
    out = []
    for line in stderr.split(b"\n"):
        if line.startswith(prefix) and TOKEN_RE.fullmatch(line[len(prefix):]):
            out.append(line[len(prefix):].decode("ascii"))
    return out


def stderr_problems(text: str, allow: Iterable[str] = ()) -> list[str]:
    """`Traceback`、ERROR/CRITICAL 行(含 `SPILLWAY-LOG ERROR …`);符合 `allow` 任一 regex 的行除外。"""
    allowed = [re.compile(a) for a in allow]
    return [ln for ln in text.splitlines()
            if _PROBLEM_RE.search(ln) and not any(a.search(ln) for a in allowed)]


class _StderrPump:
    """背景讀子行程 stderr:原始位元組留在記憶體,寫入日誌檔的副本遮掉 token。"""

    def __init__(self, stream: Any, log_path: Path) -> None:
        self._stream = stream
        self._log = log_path.open("ab")
        self._lock = threading.Lock()
        self._raw = bytearray()
        self._secrets: list[bytes] = []
        self.last_write = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="serve-stderr", daemon=True)
        self._thread.start()

    def add_secret(self, secret: str) -> None:
        with self._lock:
            if secret and secret.encode() not in self._secrets:
                self._secrets.append(secret.encode())

    def _run(self) -> None:
        prefix = TOKEN_NOTICE.encode("utf-8")
        for line in iter(self._stream.readline, b""):
            body = line.rstrip(b"\n")
            if body.startswith(prefix) and TOKEN_RE.fullmatch(body[len(prefix):]):
                self.add_secret(body[len(prefix):].decode("ascii"))
            with self._lock:
                self._raw += line
                masked = line
                for s in self._secrets:
                    masked = masked.replace(s, b"***")
                self.last_write = time.monotonic()
            self._log.write(masked)
            self._log.flush()

    def raw(self) -> bytes:
        with self._lock:
            return bytes(self._raw)

    def masked_text(self) -> str:
        with self._lock:
            data = bytes(self._raw)
            for s in self._secrets:
                data = data.replace(s, b"***")
        return data.decode("utf-8", errors="replace")

    def join(self, timeout: float = 10.0) -> bool:
        """等讀取執行緒結束(stderr 到 EOF);回傳是否已結束。已結束時關閉日誌檔。"""
        self._thread.join(timeout)
        if self._thread.is_alive():
            return False
        with contextlib.suppress(Exception):
            self._log.close()
        return True


# 存活中的 serve(F8):測試行程結束時對其行程群組 SIGKILL,避免直接使用 `Serve` 的測試失敗後殘留
_LIVE: weakref.WeakSet[Serve] = weakref.WeakSet()


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_live() -> None:
    for s in list(_LIVE):
        if s.proc is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(s.proc.pid, _signal.SIGKILL)
    _LIVE.clear()


atexit.register(_kill_live)


class Serve:
    _counter = 0

    def __init__(
        self,
        home: Path | str,
        *,
        control: bool = True,
        start_time: Any = None,
        rng_seed: int | None = None,
        adapter_paths: Sequence[Path | str] = (),
        retention_poll_seconds: float | None = None,
        retention_stepped: bool = False,
        no_token: bool = False,
        log_level: str | None = None,
        graceful_timeout: float | None = 3.0,
        mcp_max_sessions: int | None = None,
        extra_env: Mapping[str, str] | None = None,
        fake_bin: Path | None = None,
        port: int | None = None,
        log_path: Path | None = None,
        allowed_unavailable_adapters: Iterable[str] = (),
        allow_stderr: Iterable[str] = (),
    ) -> None:
        self.home = _require_tmp(Path(home))
        self.home.mkdir(parents=True, exist_ok=True)
        self.control_enabled = control
        self.start_time = start_time
        self.rng_seed = rng_seed
        self.adapter_paths = [Path(p) for p in adapter_paths]
        self.retention_poll_seconds = retention_poll_seconds
        self.retention_stepped = retention_stepped
        self.no_token = no_token
        self.log_level = log_level
        self.graceful_timeout = graceful_timeout
        self.mcp_max_sessions = mcp_max_sessions
        self.extra_env = dict(extra_env or {})
        self.fake_bin = fake_bin
        self.port = port or free_port()
        Serve._counter += 1
        self.log_path = Path(log_path) if log_path else self.home.parent / f"serve-{self.port}-{Serve._counter}.log"
        self.allowed_unavailable_adapters = frozenset(allowed_unavailable_adapters)
        self.allow_stderr = tuple(allow_stderr)
        self.token: str | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self._pump: _StderrPump | None = None
        self._control: Control | None = None
        self._stopped = True
        self._secrets: list[str] = []

    # ------------------------------------------------------------------ 環境與啟動
    def env(self) -> dict[str, str]:
        extra: dict[str, str] = {}
        if self.control_enabled:
            extra[CONTROL_ENV] = "1"
            if self.start_time is not None:
                extra[START_TIME_ENV] = iso(self.start_time)
            if self.rng_seed is not None:
                extra[RNG_SEED_ENV] = str(int(self.rng_seed))
            if self.adapter_paths:
                extra[ADAPTER_PATHS_ENV] = os.pathsep.join(str(p) for p in self.adapter_paths)
            if self.retention_poll_seconds is not None:
                extra[RETENTION_POLL_ENV] = format(float(self.retention_poll_seconds), "f")
            if self.retention_stepped:
                extra[RETENTION_STEPPED_ENV] = "1"
            if self.no_token:
                extra[NO_TOKEN_ENV] = "1"
            if self.log_level is not None:
                extra[LOG_LEVEL_ENV] = self.log_level
        if self.graceful_timeout is not None:
            extra[GRACEFUL_ENV] = str(self.graceful_timeout)
        if self.mcp_max_sessions is not None:
            extra[MCP_MAX_SESSIONS_ENV] = str(self.mcp_max_sessions)
        extra.update(self.extra_env)
        return child_env(self.home, fake_bin=self.fake_bin, extra=extra)

    def start(self, *, wait_ready: bool = True, timeout: float = 60.0) -> Serve:
        assert self.proc is None or self.proc.poll() is not None, "serve 已在執行"
        self.proc = subprocess.Popen(
            [*spillway_cmd(), "serve", "--port", str(self.port)], env=self.env(), cwd=str(self.home.parent),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            start_new_session=True)
        self._pump = _StderrPump(self.proc.stderr, self.log_path)
        for s in self._secrets:
            self._pump.add_secret(s)
        self._stopped = False
        _LIVE.add(self)
        if wait_ready:
            try:
                self.wait_ready(timeout)
                tokens = parse_tokens(self._pump.raw())
                if tokens:
                    self._remember_token(tokens[-1])
                if self.adapter_paths:
                    self._check_adapters()
            except BaseException:
                self.kill()
                raise
        return self

    def __enter__(self) -> Serve:
        return self.start() if self.proc is None else self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            self.stop()
        else:
            self.kill()

    def wait_ready(self, timeout: float = 60.0) -> None:
        assert self.proc is not None
        deadline = time.monotonic() + timeout
        warning = TEST_CONTROL_WARNING.encode("utf-8") + b"\n"
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                time.sleep(0.1)   # 讓 stderr 讀完
                raise RuntimeError(f"serve 提前結束 rc={self.proc.returncode}\n{self.stderr_text()[-4000:]}")
            if self.control_enabled and warning not in self.stderr_bytes():
                time.sleep(0.02)   # 14.3.3:警告行在開始監聽前寫出;還沒看到就不是這個 serve 在聽
                continue
            try:
                r = httpx.get(self.url("/api/settings/context"), timeout=2, trust_env=False)
                if r.status_code == 200 and self.proc.poll() is None:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        raise RuntimeError(f"serve 未在 {timeout} 秒內就緒\n{self.stderr_text()[-4000:]}")

    def _check_adapters(self) -> None:
        expected = sorted({d.name for p in self.adapter_paths if p.is_dir() for d in p.iterdir()
                           if d.is_dir() and (d / "__init__.py").is_file()})
        assert expected, f"adapter_paths 下沒有任何模組資料夾:{[str(p) for p in self.adapter_paths]}"
        with self.http() as c:
            r = c.get("/api/adapters")
        assert r.status_code == 200, r.status_code
        mods = {m["module"]: m for m in r.json()["modules"] if not m["builtin"]}
        missing = [n for n in expected if n not in mods]
        assert not missing, f"adapter_paths 的模組未被載入:{missing}(serve 列出 {sorted(mods)})"
        broken = {n: mods[n]["unavailable_reason"] for n in expected
                  if not mods[n]["available"] and n not in self.allowed_unavailable_adapters}
        assert not broken, f"adapter_paths 的模組載入失敗:{broken}"

    # ------------------------------------------------------------------ 停止與重啟
    @property
    def returncode(self) -> int | None:
        return None if self.proc is None else self.proc.poll()

    def signal(self, sig: int) -> None:
        assert self.proc is not None
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.proc.pid, sig)

    def wait_exit(self, timeout: float) -> float:
        assert self.proc is not None
        start = time.monotonic()
        self.proc.wait(timeout)
        return time.monotonic() - start

    def _finish(self, pump_timeout: float = 10.0) -> bool:
        """收尾;回傳 stderr 是否已到 EOF。"""
        eof = True if self._pump is None else self._pump.join(pump_timeout)
        if self._control is not None:
            self._control.close()
            self._control = None
        self._stopped = True
        _LIVE.discard(self)
        return eof

    def _reap_group(self, wait: float = 2.0) -> bool:
        """行程結束後輪詢行程群組至多 `wait` 秒;仍有成員時 SIGKILL 該群組並回傳 True(有殘留)。"""
        assert self.proc is not None
        deadline = time.monotonic() + wait
        while _group_alive(self.proc.pid):
            if time.monotonic() >= deadline:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self.proc.pid, _signal.SIGKILL)
                gone = time.monotonic() + 5.0   # 等孤兒被回收,呼叫端之後看到的是已清空的群組
                while _group_alive(self.proc.pid) and time.monotonic() < gone:
                    time.sleep(0.05)
                return True
            time.sleep(0.05)
        return False

    def kill(self) -> None:
        if self.proc is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.proc.pid, _signal.SIGKILL)   # 群組內的子孫行程一併
            with contextlib.suppress(Exception):
                self.proc.wait(15)
        self._finish()

    def stop(self, sig: int = _signal.SIGTERM, *, timeout: float | None = None, expect_code: int | None = None,
             allow_stderr: Iterable[str] = ()) -> int:
        if self._stopped:
            return 0 if self.proc is None else self.proc.returncode
        assert self.proc is not None
        if expect_code is None:
            expect_code = EXIT_SIGINT if sig == _signal.SIGINT else -sig
        limit = timeout if timeout is not None else (self.graceful_timeout or 30.0) + 20.0
        timed_out = False
        if self.proc.poll() is None:
            self.signal(sig)
            try:
                self.proc.wait(limit)
            except subprocess.TimeoutExpired:
                timed_out = True
                self.signal(_signal.SIGKILL)
                self.proc.wait(15)
        eof = self._pump.join(5.0) if self._pump is not None else True
        leftover = self._reap_group()
        if not eof:   # 殘留的子孫行程已被 SIGKILL,讓讀取執行緒收尾
            self._finish(5.0)
        self._finish()
        rc = self.proc.returncode
        text = self.stderr_text()
        assert not timed_out, f"serve 未在 {limit} 秒內結束(已 SIGKILL)\n{text[-4000:]}"
        assert eof, "stderr 未關閉:可能有子行程殘留(已 SIGKILL 行程群組)"
        assert not leftover, "serve 結束後行程群組仍有行程殘留(已 SIGKILL)"
        assert rc == expect_code, f"serve 結束碼 {rc},預期 {expect_code}\n{text[-4000:]}"
        problems = stderr_problems(text, (*self.allow_stderr, *allow_stderr))
        assert not problems, "serve stderr 有 Traceback 或 ERROR:\n" + "\n".join(problems[:20])
        return rc

    def restart(self, extra_env: Mapping[str, str] | None = None,
                adapter_paths: Sequence[Path | str] | None = None, **opts: Any) -> Serve:
        if self.control_enabled and self.proc is not None and self.proc.poll() is None:
            clock = self.control.clock()
            if clock["controllable"]:
                self.start_time = clock["now"]
        self.stop()
        if extra_env:
            self.extra_env.update(extra_env)
        if adapter_paths is not None:
            self.adapter_paths = [Path(p) for p in adapter_paths]
        for k, v in opts.items():
            if not hasattr(self, k) or k.startswith("_"):
                raise TypeError(f"未知的選項:{k}")
            setattr(self, k, v)
        return self.start()

    # ------------------------------------------------------------------ 位址與 HTTP
    @property
    def base_url(self) -> str:
        return f"http://{HOST}:{self.port}"

    def url(self, path: str) -> str:
        return self.base_url + path

    def mcp_url(self, group: str = "all") -> str:
        return self.url(f"/mcp/{quote(group, safe='')}")

    def auth_headers(self, token: str | None = None) -> dict[str, str]:
        tok = token if token is not None else self.token
        return {"Authorization": f"Bearer {tok}"} if tok else {}

    def http(self, **kw: Any) -> httpx.Client:
        kw.setdefault("timeout", 30.0)
        return httpx.Client(base_url=self.base_url, trust_env=False, **kw)

    def async_http(self, **kw: Any) -> httpx.AsyncClient:
        kw.setdefault("timeout", 30.0)
        return httpx.AsyncClient(base_url=self.base_url, trust_env=False, **kw)

    def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        with self.http() as c:
            return c.request(method, path, **kw)

    @property
    def control(self) -> Control:
        assert self.control_enabled, "此 serve 未開啟測試控制介面"
        if self._control is None:
            self._control = Control(self.base_url)
        return self._control

    # ------------------------------------------------------------------ 資料庫與 token
    @property
    def db_path(self) -> Path:
        return _db_path(self.home)

    def db(self) -> Any:
        return read_db(self.home)

    def _remember_token(self, token: str) -> None:
        self.token = token
        if token not in self._secrets:
            self._secrets.append(token)
        if self._pump is not None:
            self._pump.add_secret(token)

    def rotate_token(self) -> str:
        r = self.request("POST", "/api/settings/token/rotate")
        assert r.status_code == 200, r.status_code
        self._remember_token(r.json()["token"])
        assert self.token is not None
        return self.token

    def redact(self, text: str) -> str:
        return redact_tokens(text, self._secrets)

    # ------------------------------------------------------------------ MCP
    @asynccontextmanager
    async def mcp_session(self, group: str = "all", *, token: Any = _DEFAULT, headers: Mapping[str, str] | None = None,
                          message_handler: Any = None, read_timeout: float = 60.0) -> AsyncIterator[Any]:
        from tests.blackbox.mcp import McpClient, mcp_http_session

        tok = self.token if token is _DEFAULT else token
        async with mcp_http_session(self.mcp_url(group), tok, headers=headers, message_handler=message_handler,
                                    read_timeout=read_timeout) as (session, init, sid):
            yield McpClient(session=session, init=init, session_id=sid)

    def stream_open(self, session_id: str) -> bool:
        return self.control.stream_open(session_id)

    async def wait_stream_open(self, session_id: str, timeout: float = 5.0) -> None:
        import asyncio

        await asyncio.to_thread(self.control.wait_stream_open, session_id, timeout)

    # ------------------------------------------------------------------ stderr
    def stderr_bytes(self) -> bytes:
        return b"" if self._pump is None else self._pump.raw()

    def stderr_text(self) -> str:
        return "" if self._pump is None else self._pump.masked_text()

    def stderr_offset(self) -> int:
        return len(self.stderr_bytes())

    def stderr_since(self, offset: int) -> bytes:
        return self.stderr_bytes()[offset:]

    def drain_stderr(self, quiet: float = 0.3, timeout: float = 5.0) -> None:
        if self._pump is None:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if time.monotonic() - self._pump.last_write >= quiet:
                return
            time.sleep(0.05)

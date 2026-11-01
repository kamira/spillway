"""子行程執行器 `CliRunner`(CHG-20261101-03 決策 8;spec 7.6.3;design D1、D5)。

CLI 模組(`claude_cli`、`codex_cli`)不可自行起子行程,只能經 `AdapterContext.cli_runner.run()`。

`async run(argv, *, stdin=None, env_remove=(), env_set=None, cwd, timeout,
max_output_bytes=8 MiB) -> CliOutput`

- 以 `asyncio.create_subprocess_exec(..., start_new_session=True)` 啟動:argv 陣列、不經 shell;
  子行程自成 session 與 process group(pgid == pid),終端 Ctrl-C 不會直接打到它。
- 環境:以目前 `os.environ` 為底,移除 `env_remove`,再套上 `env_set`。
- `stdin` 未給時為 `DEVNULL`;給 `str` 時以 UTF-8 編碼送入後關閉。
- 逾時(`timed_out=True`、`killed_reason="timeout"`)、`run` 所在 task 被取消(`CancelledError`,
  `killed_reason="cancelled"`,例外照常往上丟)、停機(`terminate_all`/`kill_all_sync`,
  `killed_reason="shutdown"`)一律 `os.killpg` 整個 group:SIGTERM,group 在 `term_grace`
  (預設 5 秒)內沒有全部結束就 SIGKILL。孫行程在同一 group,一併結束。取消被重複遞送
  (anyio/Starlette 位準觸發的 cancel scope)時改為同步立即 SIGKILL,`CancelledError` 照常往上丟。
- 主行程結束後最多再等 `drain_timeout`(預設 2 秒)讀完管線;孫行程仍持有 stdout 時不無限等待。
  `run` 回傳前 group 若仍有殘留行程,一併終止(整個 group 屬於這一次呼叫)。
- stdout、stderr 各自超過 `max_output_bytes` 時截斷(其餘照讀照丟,不讓子行程卡在滿管線),
  以 `CliOutput.stdout_truncated`/`stderr_truncated` 標記。輸出以 UTF-8(`errors="replace"`)解碼。
- 找不到執行檔、argv 過長(E2BIG)等啟動錯誤以 `OSError` 往上丟,由模組對應成 `error`。

併發名額(決策 7;由 Router 取得、模組不碰):`try_acquire(key, limit)`(同步、不 await)、
`release(key)`、`in_flight(key)`。`active_pids`(唯讀)列出進行中子行程,供測試與診斷。

停機(決策 16):`async terminate_all(grace=None)`(lifespan 結束的後備)、`kill_all_sync(grace=None)`
(同步,給 `_run_server` 的 `finally` 與 `atexit`;事件迴圈已停時也能用)。

秘密:本模組不記錄 argv、stdin、環境變數或輸出內容。
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CliOutput",
    "CliRunner",
    "DEFAULT_DRAIN_TIMEOUT",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_TERM_GRACE",
    "KILLED_REASONS",
]

DEFAULT_TERM_GRACE = 5.0                    # SIGTERM 之後等多久才 SIGKILL(秒)
DEFAULT_DRAIN_TIMEOUT = 2.0                 # 主行程結束後讀管線的上限(秒)
DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024  # stdout、stderr 各自的上限
KILLED_REASONS = ("timeout", "cancelled", "shutdown")

_POLL = 0.02          # 等 group 結束的輪詢間隔(秒)
_READ_CHUNK = 65536


@dataclass(frozen=True)
class CliOutput:
    """一次子行程呼叫的結果。

    - `exit_code`:主行程的結束碼(被訊號終止時為負的訊號編號,同 `asyncio` 慣例)。
    - `timed_out`:是否因 `timeout` 被終止。
    - `killed_reason`:`None`(自行結束)或 `KILLED_REASONS` 之一。
    """

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    killed_reason: str | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class _Capture:
    """讀一條管線到 EOF;只保留前 `cap` bytes,超出的照讀照丟並標記。"""

    def __init__(self, cap: int) -> None:
        self.cap = max(0, int(cap))
        self.buf = bytearray()
        self.truncated = False

    async def pump(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(_READ_CHUNK)
            if not chunk:
                return
            room = self.cap - len(self.buf)
            if room > 0:
                self.buf += chunk[:room]
            if len(chunk) > room:
                self.truncated = True

    def text(self) -> str:
        return bytes(self.buf).decode("utf-8", errors="replace")


async def _feed(writer: asyncio.StreamWriter | None, data: bytes) -> None:
    if writer is None:
        return
    try:
        writer.write(data)
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # 子行程沒讀完 stdin 就結束
    finally:
        try:
            writer.close()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:   # pgid 已不屬於我們(不應發生);不碰它
        return False
    return True


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


class CliRunner:
    """共用子行程執行器;一個 app 一個實例(`app.state.cli_runner`)。"""

    def __init__(self, *, term_grace: float = DEFAULT_TERM_GRACE,
                 drain_timeout: float = DEFAULT_DRAIN_TIMEOUT) -> None:
        self.term_grace = float(term_grace)
        self.drain_timeout = float(drain_timeout)
        self._active: dict[int, asyncio.subprocess.Process] = {}   # pgid → 主行程
        self._reasons: dict[int, str] = {}                          # pgid → killed_reason
        self._slots: dict[str, int] = {}

    # ------------------------------------------------------------ 併發名額(決策 7)
    def try_acquire(self, key: str, limit: int) -> bool:
        """進行中數量 < `limit` 時佔一個名額並回 True;否則回 False(不排隊)。"""
        n = self._slots.get(key, 0)
        if n >= limit:
            return False
        self._slots[key] = n + 1
        return True

    def release(self, key: str) -> None:
        n = self._slots.get(key, 0) - 1
        if n > 0:
            self._slots[key] = n
        else:
            self._slots.pop(key, None)

    def in_flight(self, key: str) -> int:
        return self._slots.get(key, 0)

    # ------------------------------------------------------------ 執行
    @property
    def active_pids(self) -> tuple[int, ...]:
        """進行中子行程(主行程)的 pid,亦即各自的 pgid;供測試與停機診斷,呼叫端不需要它。"""
        return tuple(self._active)

    async def run(
        self,
        argv: Iterable[str],
        *,
        stdin: str | bytes | None = None,
        env_remove: Iterable[str] = (),
        env_set: Mapping[str, str] | None = None,
        cwd: str | Path | None,
        timeout: float | None,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> CliOutput:
        args = [str(a) for a in argv]
        if not args:
            raise ValueError("argv 不可為空")
        if isinstance(env_remove, str):   # 防呆:字串會被逐字元當成變數名
            raise TypeError("env_remove 必須是變數名的序列,不可為單一字串")
        env = dict(os.environ)
        for name in env_remove:
            env.pop(name, None)
        if env_set:
            env.update({str(k): str(v) for k, v in env_set.items()})
        data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            start_new_session=True,
        )
        pgid = proc.pid
        self._active[pgid] = proc
        out, err = _Capture(max_output_bytes), _Capture(max_output_bytes)
        readers = [asyncio.create_task(out.pump(proc.stdout)),
                   asyncio.create_task(err.pump(proc.stderr))]
        tasks = list(readers)
        if data is not None:
            tasks.append(asyncio.create_task(_feed(proc.stdin, data)))

        timed_out = False
        cancel_exc: asyncio.CancelledError | None = None   # 原樣往上丟(anyio 依它辨識自己的取消)
        own_reason: str | None = None
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except TimeoutError:
            timed_out, own_reason = True, "timeout"
        except asyncio.CancelledError as exc:
            cancel_exc, own_reason = exc, "cancelled"
        except BaseException:
            self._active.pop(pgid, None)
            self._reasons.pop(pgid, None)
            self._finish_sync(pgid, proc, tasks)
            raise
        # 先登出(任何 await 之前):之後不論收尾被怎樣打斷,_active/_reasons 都不會殘留
        self._active.pop(pgid, None)
        shutdown_reason = self._reasons.pop(pgid, None)
        reason = own_reason or shutdown_reason
        late = await self._cleanup(pgid, proc, readers, tasks, terminate=own_reason is not None)
        cancel_exc = cancel_exc or late
        if cancel_exc is not None:
            raise cancel_exc
        return CliOutput(
            exit_code=proc.returncode,
            stdout=out.text(),
            stderr=err.text(),
            timed_out=timed_out,
            killed_reason=reason,
            stdout_truncated=out.truncated,
            stderr_truncated=err.truncated,
        )

    async def _cleanup(self, pgid: int, proc: asyncio.subprocess.Process,
                       readers: list[asyncio.Task], tasks: list[asyncio.Task], *,
                       terminate: bool) -> asyncio.CancelledError | None:
        """收尾:(必要時)SIGTERM→grace→SIGKILL、有限時間讀完管線、終止 group 殘留行程。

        收尾在 `asyncio.shield` 內跑,邊緣觸發的取消(asyncio、uvicorn)不會打斷 SIGTERM→grace。
        取消若被重複遞送(anyio/Starlette 的 cancel scope 為位準觸發,每個 await 都會再被取消),
        就放棄等待、改以同步方式立刻 SIGKILL 整個 group 並結束;回傳期間收到的 `CancelledError`
        (沒有則為 None),由 `run` 原樣往上丟。
        """
        async def finish() -> None:
            if terminate:
                await self._terminate_group(pgid, proc, self.term_grace)
            # 主行程已結束:有限時間讀完管線(孫行程可能仍持有 stdout)
            await asyncio.wait(readers, timeout=self.drain_timeout)
            if _group_alive(pgid):   # 殘留的孫行程:整個 group 屬於這次呼叫
                await self._terminate_group(pgid, proc, self.term_grace)

        caught: asyncio.CancelledError | None = None
        job = asyncio.ensure_future(finish())
        try:
            await asyncio.shield(job)
        except asyncio.CancelledError as exc:
            caught = exc
            job.cancel()
        finally:
            self._finish_sync(pgid, proc, tasks)
        return caught

    @staticmethod
    def _finish_sync(pgid: int, proc: asyncio.subprocess.Process,
                     tasks: list[asyncio.Task]) -> None:
        """同步的最後防線(不 await):group 仍在就 SIGKILL,取消未完成的讀寫 task。"""
        if _group_alive(pgid):
            _signal_group(pgid, signal.SIGKILL)
        pending = [t for t in tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            # 管線仍被 group 外的行程(setsid 的孫行程)持有,或收尾被強制中止:asyncio 的 Process
            # 沒有公開的「關閉管線」API,不關 transport 會留下開著的 fd 與 ResourceWarning。
            # `_transport` 是 asyncio.subprocess.Process 自己實作 kill/wait 用的同一個物件。
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                transport.close()
        for t in tasks:
            if t.done() and not t.cancelled():
                t.exception()   # 取出例外,避免 "never retrieved" 警告

    async def _terminate_group(self, pgid: int, proc: asyncio.subprocess.Process,
                               grace: float) -> None:
        """SIGTERM 整個 group;`grace` 秒內沒全部結束就 SIGKILL;再等主行程被回收。"""
        _signal_group(pgid, signal.SIGTERM)
        deadline = time.monotonic() + max(0.0, grace)
        while _group_alive(pgid) and time.monotonic() < deadline:
            await asyncio.sleep(_POLL)
        if _group_alive(pgid):
            _signal_group(pgid, signal.SIGKILL)
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=max(1.0, grace))
            except TimeoutError:
                pass

    # ------------------------------------------------------------ 停機(決策 16)
    async def terminate_all(self, grace: float | None = None) -> None:
        """終止全部進行中子行程的 group(SIGTERM → `grace` → SIGKILL)。"""
        g = self.term_grace if grace is None else grace
        items = list(self._active.items())
        for pgid, _ in items:
            self._reasons.setdefault(pgid, "shutdown")
        await asyncio.gather(*(self._terminate_group(pgid, proc, g) for pgid, proc in items),
                             return_exceptions=True)

    def kill_all_sync(self, grace: float | None = None) -> None:
        """同步版:SIGTERM 全部 group,`grace` 秒內沒結束的 SIGKILL。不需要事件迴圈。"""
        g = self.term_grace if grace is None else grace
        pgids = list(self._active)
        for pgid in pgids:
            self._reasons.setdefault(pgid, "shutdown")
            _signal_group(pgid, signal.SIGTERM)
        deadline = time.monotonic() + max(0.0, g)
        while any(_group_alive(p) for p in pgids) and time.monotonic() < deadline:
            time.sleep(_POLL)
        for pgid in pgids:
            if _group_alive(pgid):
                _signal_group(pgid, signal.SIGKILL)

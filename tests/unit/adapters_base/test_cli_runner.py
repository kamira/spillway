"""CliRunner(CHG-20261101-03 T2;決策 8)與子行程禁用的分層規則。

子行程一律是 `sys.executable -c` 小腳本(不執行真實 claude/codex)。需要長時間等待的情境以
小的 `term_grace`/`drain_timeout` 建立 runner,預設值(5 秒/2 秒)另以常數斷言。
每個會留下行程的測試都把 pid 寫進檔案,結束時以 `os.kill(pid, 0)` 與 `pgrep -f <marker>` 斷言沒有殘留。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import anyio
import pytest

from spillway.adapters.base import (
    DEFAULT_DRAIN_TIMEOUT,
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_TERM_GRACE,
    AdapterContext,
    CliOutput,
    CliRunner,
    layering_violations,
)

pytestmark = pytest.mark.anyio

PY = sys.executable
GRACE = 0.5
DRAIN = 0.5

# 子行程腳本:argv[1] 為 pid 檔目錄,argv[2] 為 marker(給 pgrep)
SPAWN_GRANDCHILD = r"""
import os, signal, subprocess, sys, time
d, marker = sys.argv[1], sys.argv[2]
mode = sys.argv[3] if len(sys.argv) > 3 else ""
open(os.path.join(d, "child.pid"), "w").write(str(os.getpid()))
gc_code = (
    "import os, signal, sys, time\n"
    "mode = sys.argv[2]\n"
    "if 'gc_escape' in mode: os.setsid()\n"
    "if 'gc_ignore_term' in mode: signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "open(os.path.join(sys.argv[1], 'grandchild.pid'), 'w').write(str(os.getpid()))\n"
    "time.sleep(60)\n"
)
subprocess.Popen([sys.executable, "-c", gc_code, d, mode, marker])  # 繼承 stdout
while not os.path.exists(os.path.join(d, "grandchild.pid")):
    time.sleep(0.01)
if "child_ignore_term" in mode:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("ready", flush=True)
if "child_exits" in mode:
    sys.exit(0)
time.sleep(60)
"""


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(term_grace=GRACE, drain_timeout=DRAIN)


@pytest.fixture
def marker() -> str:
    return f"spillway-t2-{uuid.uuid4().hex}"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_dead(pids, timeout: float = 3.0) -> list[int]:
    """回傳 `timeout` 後仍存在的 pid(孫行程交給 launchd/init 回收需要一點時間)。"""
    deadline = time.monotonic() + timeout
    left = [p for p in pids if _alive(p)]
    while left and time.monotonic() < deadline:
        time.sleep(0.02)
        left = [p for p in left if _alive(p)]
    return left


def _pgrep(marker: str) -> list[str]:
    if shutil.which("pgrep") is None:
        return []
    r = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    return [x for x in r.stdout.split() if x and int(x) != os.getpid()]


def _read_pids(d: Path) -> list[int]:
    return [int((d / f).read_text()) for f in ("child.pid", "grandchild.pid") if (d / f).exists()]


async def _wait_files(d: Path, names=("child.pid", "grandchild.pid"), timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not all((d / n).exists() and (d / n).read_text() for n in names):
        assert time.monotonic() < deadline, "子行程沒有啟動"
        await asyncio.sleep(0.02)


@pytest.fixture
def piddir(tmp_path: Path, marker: str):
    d = tmp_path / "pids"
    d.mkdir()
    yield d
    # 保險:測試失敗時也不留下行程
    for pid in _read_pids(d):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _assert_no_leftovers(d: Path, marker: str) -> None:
    pids = _read_pids(d)
    assert pids, "測試沒有記錄到子行程 pid"
    assert _wait_dead(pids) == []
    assert _pgrep(marker) == []


# ================================================================ 基本行為

def test_defaults():
    assert DEFAULT_TERM_GRACE == 5.0
    assert DEFAULT_DRAIN_TIMEOUT == 2.0
    assert DEFAULT_MAX_OUTPUT_BYTES == 8 * 1024 * 1024
    r = CliRunner()
    assert (r.term_grace, r.drain_timeout) == (5.0, 2.0)


async def test_env_remove_and_env_set(runner, monkeypatch):
    monkeypatch.setenv("T2_REMOVE_ME", "x")
    monkeypatch.setenv("T2_KEEP_ME", "k")
    code = ("import json, os; print(json.dumps({k: os.environ.get(k) for k in "
            "('T2_REMOVE_ME', 'T2_KEEP_ME', 'SPILLWAY_DEPTH')}))")
    out = await runner.run([PY, "-c", code], env_remove=("T2_REMOVE_ME", "T2_ABSENT"),
                           env_set={"SPILLWAY_DEPTH": "1"}, cwd=None, timeout=10)
    assert isinstance(out, CliOutput)
    assert out.exit_code == 0 and not out.timed_out and out.killed_reason is None
    assert json.loads(out.stdout) == {"T2_REMOVE_ME": None, "T2_KEEP_ME": "k", "SPILLWAY_DEPTH": "1"}
    assert os.environ["T2_REMOVE_ME"] == "x"   # 不動自己的環境


async def test_stdin_delivered(runner):
    code = "import sys; data = sys.stdin.read(); print(len(data)); print(data, end='')"
    out = await runner.run([PY, "-c", code], stdin="héllo\nworld", cwd=None, timeout=10)
    assert out.stdout == "11\nhéllo\nworld"


async def test_stdin_defaults_to_devnull(runner):
    code = "import sys; print(repr(sys.stdin.read()))"
    out = await runner.run([PY, "-c", code], cwd=None, timeout=10)
    assert out.exit_code == 0 and out.stdout.strip() == "''"


async def test_cwd(runner, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    out = await runner.run([PY, "-c", "import os; print(os.getcwd())"], cwd=work, timeout=10)
    assert Path(out.stdout.strip()).resolve() == work.resolve()


async def test_new_session_and_process_group(runner):
    code = "import os; print(os.getpid(), os.getsid(0), os.getpgid(0))"
    out = await runner.run([PY, "-c", code], cwd=None, timeout=10)
    pid, sid, pgid = map(int, out.stdout.split())
    assert sid == pid and pgid == pid
    assert sid != os.getsid(0) and pgid != os.getpgid(0)


async def test_exit_code_and_stderr(runner):
    code = "import sys; sys.stderr.write('boom'); sys.exit(3)"
    out = await runner.run([PY, "-c", code], cwd=None, timeout=10)
    assert out.exit_code == 3 and out.stderr == "boom" and out.stdout == ""
    assert out.killed_reason is None and not out.timed_out


async def test_missing_executable_raises_oserror(runner, tmp_path):
    with pytest.raises(OSError):
        await runner.run([str(tmp_path / "no-such-cli")], cwd=None, timeout=5)
    assert runner.active_pids == ()


async def test_output_cap_truncates_and_flags(runner):
    code = ("import sys; sys.stdout.write('a' * 300000); sys.stdout.flush(); "
            "sys.stderr.write('b' * 300000)")
    out = await runner.run([PY, "-c", code], cwd=None, timeout=10, max_output_bytes=1000)
    assert out.exit_code == 0           # 子行程沒有卡在滿管線
    assert out.stdout == "a" * 1000 and out.stdout_truncated
    assert out.stderr == "b" * 1000 and out.stderr_truncated
    small = await runner.run([PY, "-c", "print('ok')"], cwd=None, timeout=10)
    assert not small.stdout_truncated and not small.stderr_truncated


# ================================================================ 逾時與 process group

async def test_timeout_kills_child_and_grandchild(runner, piddir, marker):
    t0 = time.monotonic()
    out = await runner.run([PY, "-c", SPAWN_GRANDCHILD, str(piddir), marker], cwd=None, timeout=1.0)
    elapsed = time.monotonic() - t0
    assert out.timed_out and out.killed_reason == "timeout"
    assert out.exit_code == -signal.SIGTERM
    assert "ready" in out.stdout
    assert elapsed < 1.0 + 2.0
    _assert_no_leftovers(piddir, marker)
    assert runner.active_pids == ()


async def test_sigterm_ignored_escalates_to_sigkill(runner, piddir, marker):
    t0 = time.monotonic()
    out = await runner.run([PY, "-c", SPAWN_GRANDCHILD, str(piddir), marker,
                            "child_ignore_term,gc_ignore_term"], cwd=None, timeout=1.0)
    elapsed = time.monotonic() - t0
    assert out.timed_out and out.exit_code == -signal.SIGKILL
    assert 1.0 + GRACE * 0.8 <= elapsed < 1.0 + GRACE + 2.0
    _assert_no_leftovers(piddir, marker)


async def test_grandchild_holding_stdout_bounded_drain_after_normal_exit(runner, piddir, marker):
    """主行程正常結束、同 group 的孫行程仍持有 stdout:最多等 drain_timeout,之後收掉整個 group。"""
    t0 = time.monotonic()
    out = await runner.run([PY, "-c", SPAWN_GRANDCHILD, str(piddir), marker, "child_exits"],
                           cwd=None, timeout=30)
    elapsed = time.monotonic() - t0
    assert out.exit_code == 0 and not out.timed_out and out.killed_reason is None
    assert "ready" in out.stdout
    assert elapsed < DRAIN + GRACE + 2.0
    _assert_no_leftovers(piddir, marker)


async def test_escaped_grandchild_holding_stdout_does_not_block_after_timeout(runner, piddir, marker):
    """孫行程離開 group(setsid)且持有 stdout:killpg 打不到它,run 仍在逾時 + drain 內回傳。"""
    t0 = time.monotonic()
    out = await runner.run([PY, "-c", SPAWN_GRANDCHILD, str(piddir), marker, "gc_escape"],
                           cwd=None, timeout=1.0)
    elapsed = time.monotonic() - t0
    assert out.timed_out and out.killed_reason == "timeout"
    assert elapsed < 1.0 + DRAIN + 1.5
    # 離開 group 的孫行程不屬於 runner;這裡自行收掉,再斷言沒有殘留
    gc = int((piddir / "grandchild.pid").read_text())
    os.kill(gc, signal.SIGKILL)
    _assert_no_leftovers(piddir, marker)


async def test_default_grace_timeout_plus_two_seconds(piddir, marker):
    """預設參數:SIGTERM 生效時,孫行程持有 stdout 的逾時呼叫在逾時 + 2 秒內回傳。"""
    out_t0 = time.monotonic()
    out = await CliRunner().run([PY, "-c", SPAWN_GRANDCHILD, str(piddir), marker],
                                cwd=None, timeout=0.5)
    assert out.timed_out
    assert time.monotonic() - out_t0 < 0.5 + 2.0
    _assert_no_leftovers(piddir, marker)


# ================================================================ 取消與停機

async def test_cancel_kills_group(runner, piddir, marker):
    task = asyncio.create_task(runner.run([PY, "-c", SPAWN_GRANDCHILD, str(piddir), marker],
                                          cwd=None, timeout=60))
    await _wait_files(piddir)
    assert len(runner.active_pids) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner.active_pids == ()
    _assert_no_leftovers(piddir, marker)


async def test_cancel_with_sigterm_ignored_still_kills(runner, piddir, marker):
    task = asyncio.create_task(runner.run([PY, "-c", SPAWN_GRANDCHILD, str(piddir), marker,
                                           "child_ignore_term,gc_ignore_term"], cwd=None, timeout=60))
    await _wait_files(piddir)
    await asyncio.sleep(0.1)   # 讓子行程裝好 SIG_IGN
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _assert_no_leftovers(piddir, marker)


def _group_gone(pgid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:   # macOS:group 只剩尚未回收的 zombie 時回 EPERM;再等回收
            pass
        time.sleep(0.02)
    return False


def _assert_runner_clean(runner: CliRunner, d: Path, marker: str) -> None:
    assert runner.active_pids == ()
    assert runner._reasons == {}
    child = int((d / "child.pid").read_text())
    assert _group_gone(child), "process group 仍有殘留行程"
    _assert_no_leftovers(d, marker)


async def test_anyio_move_on_after_level_triggered_cancel(piddir, marker):
    """anyio cancel scope 會在每個 await 重新遞送取消:收尾不可卡住,也不可留下行程或登記。

    term_grace 設 5 秒且子、孫行程都不理 SIGTERM:若收尾仍在等 grace,經過時間會超過 5 秒。
    """
    runner = CliRunner(term_grace=5.0, drain_timeout=DRAIN)
    t0 = time.monotonic()
    with anyio.move_on_after(1.0) as scope:
        await runner.run([PY, "-c", SPAWN_GRANDCHILD, str(piddir), marker,
                          "child_ignore_term,gc_ignore_term"], cwd=None, timeout=60)
    assert scope.cancelled_caught
    assert time.monotonic() - t0 < 1.0 + 2.0
    _assert_runner_clean(runner, piddir, marker)


async def test_anyio_nested_cancel_scope(runner, piddir, marker):
    """外層 scope 取消、內層 scope 包住 run:CancelledError 往上丟到外層,group 被收掉。"""
    with anyio.CancelScope() as outer:
        async with anyio.create_task_group() as tg:
            async def call():
                with anyio.CancelScope():
                    await runner.run([PY, "-c", SPAWN_GRANDCHILD, str(piddir), marker],
                                     cwd=None, timeout=60)
            tg.start_soon(call)
            await _wait_files(piddir)
            outer.cancel()
    assert outer.cancelled_caught
    _assert_runner_clean(runner, piddir, marker)


async def test_asyncio_cancel_keeps_term_grace(piddir, marker):
    """一次性(邊緣觸發)取消:收尾在 shield 內完成 SIGTERM→grace→SIGKILL。"""
    runner = CliRunner(term_grace=GRACE, drain_timeout=DRAIN)
    task = asyncio.create_task(runner.run([PY, "-c", SPAWN_GRANDCHILD, str(piddir), marker,
                                           "child_ignore_term"], cwd=None, timeout=60))
    await _wait_files(piddir)
    await asyncio.sleep(0.1)
    t0 = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - t0 >= GRACE * 0.8   # 有等 grace,不是直接 SIGKILL
    _assert_runner_clean(runner, piddir, marker)


async def test_env_remove_rejects_bare_string(runner):
    with pytest.raises(TypeError):
        await runner.run([PY, "-c", "pass"], env_remove="PATH", cwd=None, timeout=5)
    assert runner.active_pids == ()


async def _start_two(runner, tmp_path, marker):
    dirs = [tmp_path / "a", tmp_path / "b"]
    tasks = []
    for d in dirs:
        d.mkdir()
        tasks.append(asyncio.create_task(runner.run([PY, "-c", SPAWN_GRANDCHILD, str(d), marker],
                                                    cwd=None, timeout=60)))
    for d in dirs:
        await _wait_files(d)
    assert len(runner.active_pids) == 2
    return dirs, tasks


async def test_terminate_all(runner, tmp_path, marker):
    dirs, tasks = await _start_two(runner, tmp_path, marker)
    try:
        await runner.terminate_all(grace=GRACE)
        outs = await asyncio.wait_for(asyncio.gather(*tasks), 5)
    finally:
        for d in dirs:
            for pid in _read_pids(d):
                if _alive(pid):
                    os.kill(pid, signal.SIGKILL)
    assert all(o.killed_reason == "shutdown" and not o.timed_out for o in outs)
    for d in dirs:
        _assert_no_leftovers(d, marker)
    assert runner.active_pids == ()


async def test_kill_all_sync(runner, tmp_path, marker):
    dirs, tasks = await _start_two(runner, tmp_path, marker)
    try:
        runner.kill_all_sync(grace=GRACE)   # 同步:回傳時 group 已收到訊號
        outs = await asyncio.wait_for(asyncio.gather(*tasks), 5)
    finally:
        for d in dirs:
            for pid in _read_pids(d):
                if _alive(pid):
                    os.kill(pid, signal.SIGKILL)
    assert all(o.killed_reason == "shutdown" for o in outs)
    for d in dirs:
        _assert_no_leftovers(d, marker)


def test_kill_all_sync_without_event_loop(tmp_path, marker):
    """atexit / 強制退出時事件迴圈已停:kill_all_sync 仍能收掉 group。"""
    runner = CliRunner(term_grace=GRACE, drain_timeout=DRAIN)
    d = tmp_path / "pids"
    d.mkdir()
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(runner.run([PY, "-c", SPAWN_GRANDCHILD, str(d), marker,
                                            "child_ignore_term"], cwd=None, timeout=60))
        loop.run_until_complete(_wait_files(d))
        loop.run_until_complete(asyncio.sleep(0.1))
        runner.kill_all_sync(grace=GRACE)
        _assert_no_leftovers(d, marker)
        out = loop.run_until_complete(task)
        assert out.killed_reason == "shutdown" and out.exit_code == -signal.SIGKILL
    finally:
        loop.close()
        for pid in _read_pids(d):
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)


# ================================================================ 併發名額

def test_try_acquire_release_in_flight():
    r = CliRunner()
    assert r.in_flight("dep-1") == 0
    assert r.try_acquire("dep-1", 1) is True
    assert r.in_flight("dep-1") == 1
    assert r.try_acquire("dep-1", 1) is False
    assert r.try_acquire("dep-2", 1) is True          # 名額依 key 分開
    r.release("dep-1")
    assert r.in_flight("dep-1") == 0
    assert r.try_acquire("dep-1", 1) is True
    assert r.try_acquire("dep-3", 2) and r.try_acquire("dep-3", 2) and not r.try_acquire("dep-3", 2)
    r.release("dep-3"), r.release("dep-3"), r.release("dep-3")   # 多放不會變負數
    assert r.in_flight("dep-3") == 0
    assert r.try_acquire("dep-4", 0) is False


# ================================================================ L2:app 注入

async def test_create_app_injects_same_cli_runner():
    """`app.state.cli_runner` 與 AdapterContext.cli_runner 為同一實例;lifespan 重進仍同一個;不同 app 不共用。"""
    from spillway.app import create_app

    app = create_app()
    runner = app.state.cli_runner
    assert isinstance(runner, CliRunner)
    async with app.router.lifespan_context(app):
        ctx = app.state.registry._context
        assert isinstance(ctx, AdapterContext)
        assert ctx.cli_runner is runner is app.state.cli_runner
    async with app.router.lifespan_context(app):
        assert app.state.registry._context.cli_runner is runner
    other = create_app()
    async with other.router.lifespan_context(other):
        assert other.state.cli_runner is not runner
        assert other.state.registry._context.cli_runner is other.state.cli_runner


def test_adapter_context_cli_runner_defaults_to_none(clock):
    assert AdapterContext(credentials=lambda ref: "", clock=clock).cli_runner is None


# ================================================================ L2:子行程禁用的分層規則

def _layering(tmp_path, src: str) -> list[str]:
    d = tmp_path / "fake_mod"
    d.mkdir()
    (d / "__init__.py").write_text("", encoding="utf-8")
    (d / "adapter.py").write_text(src, encoding="utf-8")
    return layering_violations(d, "spillway.adapters.fake_mod")


@pytest.mark.parametrize(
    "src",
    [
        "import os\nos.system('ls')\n",
        "import subprocess\n",
        "import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec('x')\n",
        "from subprocess import run\n",
        "import subprocess as sp\n",
        "import asyncio.subprocess\n",
        "from asyncio import create_subprocess_shell\n",
        "import os\nos.popen('ls')\n",
        "import os\nos.execv('/bin/x', ['x'])\n",
        "import os\nos.spawnl(os.P_WAIT, '/bin/x', 'x')\n",
        "import os\nos.posix_spawn('/bin/x', ['x'], {})\n",
        "import os as o\no.system('ls')\n",
        "from os import system\n",
        "from os import execvp as e\n",
        "import pty\n",
        "import importlib\nimportlib.import_module('subprocess')\n",
        "import os.path\nos.system('ls')\n",
        "import os.path as p\nimport os\nos.system('ls')\n",
        "import os.path as p\nos.system('ls')\n",
        "import asyncio\nasync def f():\n    await asyncio.get_running_loop().subprocess_exec(None, 'x')\n",
        "import asyncio\nasync def f(loop):\n    await loop.subprocess_shell(None, 'x')\n",
        "import multiprocessing\n",
        "from multiprocessing import Process\n",
        "import multiprocessing.pool\n",
        "import concurrent.futures\nconcurrent.futures.ProcessPoolExecutor()\n",
        "from concurrent.futures import ProcessPoolExecutor\n",
        "import concurrent.futures as cf\ncf.ProcessPoolExecutor()\n",
        "import posix\nposix.system('ls')\n",
        "import posix\nposix.posix_spawn('/bin/x', ['x'], {})\n",
        "from posix import system\n",
        "import os\nos.fork()\n",
        "import os\nos.forkpty()\n",
        "from os import fork\n",
    ],
)
def test_subprocess_use_is_a_layering_violation(tmp_path, src):
    v = _layering(tmp_path, src)
    assert v and all("cli_runner" in x for x in v), v


@pytest.mark.parametrize(
    "src",
    [
        "import asyncio\n",
        "import tempfile\nfd, path = tempfile.mkstemp()\n",
        "import os\nos.unlink('x')\nos.environ.get('A')\nos.path.join('a', 'b')\n",
        "import platform\nplatform.system()\n",
        "import asyncio\nasyncio.sleep(0)\n",
        "import os.path\nos.path.join('a', 'b')\n",
        "import concurrent.futures\nconcurrent.futures.ThreadPoolExecutor()\n",
        "import posix\nposix.getpid()\n",
    ],
)
def test_allowed_stdlib_use_passes(tmp_path, src):
    assert _layering(tmp_path, src) == []


@pytest.mark.parametrize("closer", ["kill_all_sync", "terminate_all"])
async def test_shutdown_during_spawn_window_kills_new_child(runner, tmp_path, marker, monkeypatch,
                                                            closer):
    """終審 D1:子行程已 fork、pgid 尚未登記進 `_active` 時停機 → spawn 一回來就 SIGKILL 該 group。"""
    real_spawn = asyncio.create_subprocess_exec
    d = tmp_path / "pids"
    d.mkdir()

    async def spawn_then_shutdown(*args, **kwargs):
        proc = await real_spawn(*args, **kwargs)
        await _wait_files(d)   # 子行程與孫行程都已起來(同一 group)
        if closer == "kill_all_sync":
            runner.kill_all_sync(grace=GRACE)   # 此時 _active 還沒有它
        else:
            await runner.terminate_all(grace=GRACE)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_then_shutdown)
    try:
        out = await asyncio.wait_for(
            runner.run([PY, "-c", SPAWN_GRANDCHILD, str(d), marker], cwd=None, timeout=30), 10)
    finally:
        for pid in _read_pids(d):
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
    assert out.killed_reason == "shutdown" and not out.timed_out
    assert out.exit_code == -signal.SIGKILL
    _assert_no_leftovers(d, marker)
    assert runner.active_pids == ()


# ---- CHG-20261107-02:以 returncode 判斷主行程結束(Python 3.11、3.12 的 wait() 會等管線) ----------

class _FakeProc:
    """只有 `returncode` 的假 proc;`wait()` 一律失敗(`_wait_exited` 不可呼叫它)。"""

    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode

    async def wait(self) -> int:
        raise AssertionError("_wait_exited 不可呼叫 Process.wait()")


def _set_later(proc: _FakeProc, rc: int, delay: float = 0.05) -> None:
    asyncio.get_running_loop().call_later(delay, setattr, proc, "returncode", rc)


async def test_wait_exited_returns_after_returncode_set():
    from spillway.adapters.base.cli_runner import _wait_exited
    proc = _FakeProc()
    _set_later(proc, 0)
    await _wait_exited(proc, 5.0)
    assert proc.returncode == 0


@pytest.mark.parametrize("timeout", [0.1, 0, -1, float("nan")])
async def test_wait_exited_times_out(timeout: float):
    from spillway.adapters.base.cli_runner import _wait_exited
    with pytest.raises(TimeoutError):
        await _wait_exited(_FakeProc(), timeout)


async def test_wait_exited_already_exited_with_zero_timeout():
    from spillway.adapters.base.cli_runner import _wait_exited
    await _wait_exited(_FakeProc(0), 0)


async def test_wait_exited_none_timeout_waits():
    from spillway.adapters.base.cli_runner import _wait_exited
    proc = _FakeProc()
    _set_later(proc, 3)
    await _wait_exited(proc, None)
    assert proc.returncode == 3


async def test_wait_exited_propagates_cancel():
    from spillway.adapters.base.cli_runner import _wait_exited
    task = asyncio.ensure_future(_wait_exited(_FakeProc(), None))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


@pytest.fixture
def hanging_wait(monkeypatch):
    """模擬 3.11/3.12:`Process.wait()` 不返回(版本無關的守衛)。"""
    async def never(self):
        await asyncio.Event().wait()
    monkeypatch.setattr(asyncio.subprocess.Process, "wait", never)


async def test_guard_normal_exit_does_not_wait_for_pipes(hanging_wait, runner, piddir, marker):
    """G1:子行程正常結束、孫行程持有 stdout,`wait()` 不返回時 run 仍以正常結束回傳。"""
    out = await asyncio.wait_for(
        runner.run([PY, "-c", SPAWN_GRANDCHILD, str(piddir), marker, "child_exits"], cwd=None, timeout=30), 10)
    assert out.exit_code == 0 and not out.timed_out and out.killed_reason is None
    _assert_no_leftovers(piddir, marker)


async def test_guard_shutdown_with_escaped_grandchild(hanging_wait, runner, piddir, marker):
    """G2:停機時逃出 group 的孫行程持有 stdout,`wait()` 不返回時 run 仍記為 shutdown、不記為逾時。"""
    task = asyncio.ensure_future(
        runner.run([PY, "-c", SPAWN_GRANDCHILD, str(piddir), marker, "gc_escape"], cwd=None, timeout=30))
    try:
        await _wait_files(piddir)
        await runner.terminate_all(grace=GRACE)
        out = await asyncio.wait_for(task, 10)
    finally:
        gc = piddir / "grandchild.pid"
        if gc.exists() and _alive(int(gc.read_text())):
            os.kill(int(gc.read_text()), signal.SIGKILL)
    assert out.killed_reason == "shutdown" and not out.timed_out
    _assert_no_leftovers(piddir, marker)


async def test_terminate_group_waits_on_returncode_not_wait(monkeypatch):
    """S1(`_terminate_group` 的結構測試):SIGKILL 之後以 `returncode` 判斷,不呼叫 `wait()`。"""
    from spillway.adapters.base import cli_runner as cr
    monkeypatch.setattr(cr, "_signal_group", lambda pgid, sig: None)
    monkeypatch.setattr(cr, "_group_alive", lambda pgid: False)
    proc = _FakeProc()
    _set_later(proc, -signal.SIGKILL)
    await asyncio.wait_for(CliRunner(term_grace=0.1)._terminate_group(12345, proc, 5.0), 3)
    assert proc.returncode == -signal.SIGKILL


SPAWN_GC_DEVNULL = r"""
import os, subprocess, sys, time
d = sys.argv[1]
open(os.path.join(d, "child.pid"), "w").write(str(os.getpid()))
code = "import os, sys, time\nopen(os.path.join(sys.argv[1], 'grandchild.pid'), 'w').write(str(os.getpid()))\ntime.sleep(60)\n"
subprocess.Popen([sys.executable, "-c", code, d, sys.argv[2]],
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
while not os.path.exists(os.path.join(d, "grandchild.pid")):
    time.sleep(0.01)
print("ready", flush=True)
"""


async def test_same_group_grandchild_without_stdout_is_terminated(runner, piddir, marker):
    """同 group 的孫行程不持有 stdout:子行程正常結束後,殘留的孫行程仍被收掉(design D29)。"""
    out = await asyncio.wait_for(
        runner.run([PY, "-c", SPAWN_GC_DEVNULL, str(piddir), marker], cwd=None, timeout=30), 10)
    assert out.exit_code == 0 and not out.timed_out and out.killed_reason is None
    assert "ready" in out.stdout
    _assert_no_leftovers(piddir, marker)


# ---- 終審 I1a:CLI 模組共用的 params.timeout 驗證 ----------------------------------------------

@pytest.mark.parametrize("v", [0, -1, -0.5, True, False, float("nan"), float("inf"),
                               float("-inf"), "abc", [1], {}])
def test_cli_timeout_rejects(v):
    from spillway.adapters.base import cli_timeout
    with pytest.raises(ValueError, match="params.timeout 必須是正數"):
        cli_timeout(v)


@pytest.mark.parametrize("v, expected", [(None, None), (1, 1.0), (0.25, 0.25), ("3", 3.0)])
def test_cli_timeout_accepts(v, expected):
    from spillway.adapters.base import cli_timeout
    assert cli_timeout(v) == expected

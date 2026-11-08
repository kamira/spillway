"""graceful shutdown(CHG-20261101-03 T8;決策 16、圖 4)。

L1(行程內):`_run_server` 傳給 uvicorn 的 `timeout_graceful_shutdown` 預設 30(測試用環境變數
`SPILLWAY_TEST_GRACEFUL_TIMEOUT` 可覆寫);第一個訊號後 `app.state.shutting_down` 為真、仍交給 uvicorn
原本的 `handle_exit`(第二次 SIGINT 為 `force_exit`);旗標為真時新進的 task 請求回 503 `shutting_down`、
不啟動子行程。
L2(子行程 `spillway serve`,`SPILLWAY_TEST_GRACEFUL_TIMEOUT=2`,環境一律經 `safe_subprocess_env`,
資料目錄為暫存 `SPILLWAY_HOME`;claude-cli 成員與 task route 經管理 API 建立;假 claude
`hold_until_release` 不放行):
① SIGTERM → serve 在 2 + 5 + 3 秒內結束、帳本該成員一列 `error="shutdown"`、假 claude 的 process group
  已不存在(`pgrep -g`);停機期間新進請求不被服務(serve 已停止接新連線則連線失敗,否則 503),
  且不會再啟動假 claude;
② `os.killpg` 對 serve 的 process group 送 SIGINT(模擬終端 Ctrl-C):假 claude 自成 session,不因此立即
  結束,其後同 ①;
③ 連送兩次 SIGINT(force_exit,略過 lifespan):serve 結束後找不到假行程;
④ SIGTERM 後再 SIGINT(同樣 force_exit;uvicorn 最後以預設處置重送 SIGTERM):不留假行程;
③④ 以停機上限 20 秒的 serve 執行,斷言 force_exit 立即生效(遠小於 20 秒)。

停機期間新進請求回 503 只在行程內驗證(`test_new_task_request_while_shutting_down_is_503`):子行程中
uvicorn 收到訊號後隨即關閉監聽 socket 與閒置連線,新請求到不了 app(連線失敗),L2 只斷言「不被服務、
不啟動新的假 claude」。
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from spillway import cli
from spillway.adapters.base import DEFAULT_TERM_GRACE
from spillway.store import repo
from spillway.store.db import connect
from tests.fakes.cli_upstream import FakeCli, install, safe_subprocess_env

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
REAL_KEY_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                 "CODEX_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
GRACEFUL = 2.0
SLOW_GRACEFUL = 20.0   # force_exit 測試:必須遠早於這個上限結束
TERM_GRACE = DEFAULT_TERM_GRACE   # CliRunner SIGTERM→SIGKILL 寬限
SLACK = 3.0
TASK_BODY = {"model": "t", "messages": [{"role": "user", "content": "do it"}],
             "metadata": {"mode": "task"}}
CLAUDE_MEMBER = {
    "id": "claude-sub", "model_name": "claude-sub", "kind": "cli", "provider": "claude-cli",
    "model": "claude-sonnet-5-5", "params": {"max_concurrency": 1, "timeout": 600},
    "capabilities": {"ctx": 1000000, "tools": False, "vision": False,
                     "efforts": ["low", "medium", "high", "max"]},
    "tags": ["t"],
}
ROUTE = {"name": "t", "target": {"tags": {"all": ["t"]}}, "mode": "task"}


# ---- L1:行程內 ----------------------------------------------------------------------------

class _Captured(Exception):
    pass


def _capture_server(monkeypatch) -> dict[str, Any]:
    """讓 `_run_server` 跳過前置檢查,並把 `server.run()` 換成記錄 server 後丟例外(不真的啟動)。"""
    import uvicorn

    seen: dict[str, Any] = {}

    def fake_run(self, sockets=None):
        seen["server"] = self
        raise _Captured

    monkeypatch.setattr(cli, "_preflight", lambda *a, **k: 0)
    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    return seen


def test_run_server_default_graceful_timeout_is_30(monkeypatch):
    seen = _capture_server(monkeypatch)
    with pytest.raises(_Captured):
        cli._run_server("127.0.0.1", 8787)
    assert seen["server"].config.timeout_graceful_shutdown == 30


def test_test_only_env_overrides_graceful_timeout(monkeypatch):
    seen = _capture_server(monkeypatch)
    monkeypatch.setenv(cli.TEST_GRACEFUL_TIMEOUT_ENV, "2")
    with pytest.raises(_Captured):
        cli.main(["serve", "--port", "8787"])
    assert seen["server"].config.timeout_graceful_shutdown == 2
    for bad in ("abc", "0", "-1", ""):
        monkeypatch.setenv(cli.TEST_GRACEFUL_TIMEOUT_ENV, bad)
        assert cli._test_graceful_timeout() is None


def test_first_signal_sets_shutting_down_and_second_sigint_forces_exit(monkeypatch):
    seen = _capture_server(monkeypatch)
    with pytest.raises(_Captured):
        cli._run_server("127.0.0.1", 8787)
    server = seen["server"]
    app = server.config.app
    assert app.state.shutting_down is False                  # 預設值由 create_app 設定(T6)
    server.handle_exit(signal.SIGTERM, None)
    assert app.state.shutting_down is True
    assert server.should_exit is True and server.force_exit is False
    server.handle_exit(signal.SIGINT, None)                  # uvicorn 的 double Ctrl-C 行為保留
    assert server.force_exit is True


def test_finally_kills_all_children(monkeypatch):
    """`server.run()` 不論怎樣結束(例如第二次 Ctrl-C 後的 KeyboardInterrupt),finally 都呼叫 kill_all_sync。"""
    from spillway.adapters.base import CliRunner

    calls: list[str] = []
    monkeypatch.setattr(CliRunner, "kill_all_sync", lambda self, grace=None: calls.append("kill"))
    _capture_server(monkeypatch)
    with pytest.raises(_Captured):
        cli._run_server("127.0.0.1", 8787)
    assert calls == ["kill"]


def test_capture_signals_kills_children_before_uvicorn_reraises(monkeypatch):
    """`capture_signals` 離開時先 `kill_all_sync()`,之後 uvicorn 才重送訊號。"""
    from spillway.adapters.base import CliRunner

    order: list[str] = []
    monkeypatch.setattr(CliRunner, "kill_all_sync", lambda self, grace=None: order.append("kill"))
    monkeypatch.setattr(signal, "raise_signal", lambda sig: order.append(f"raise {sig}"))
    seen = _capture_server(monkeypatch)
    with pytest.raises(_Captured):
        cli._run_server("127.0.0.1", 8787)
    order.clear()
    server = seen["server"]
    with server.capture_signals():
        server.handle_exit(signal.SIGTERM, None)
    assert order == ["kill", f"raise {signal.SIGTERM}"]


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_key_env")
async def test_new_task_request_while_shutting_down_is_503(spillway, fake_cli, monkeypatch):
    import uvicorn

    h = spillway
    r = await h.client.post("/api/deployments", json=CLAUDE_MEMBER)
    assert r.status_code == 201, r.text
    r = await h.client.post("/api/routes", json=ROUTE)
    assert r.status_code == 201, r.text
    server = cli._graceful_server(uvicorn.Config(h.app), h.app)
    monkeypatch.setattr(signal, "raise_signal", lambda sig: None)   # 保險:不對測試行程送訊號
    server.handle_exit(signal.SIGTERM, None)

    resp = await h.client.post("/v1/chat/completions", json=TASK_BODY)
    assert resp.status_code == 503, resp.text
    assert "shutting_down" in resp.text
    assert fake_cli.records("claude") == []                  # 沒有啟動子行程
    assert h.ledger() == []                                  # 沒有開始嘗試


# ---- L2:子行程 ----------------------------------------------------------------------------

skip_without_pgrep = pytest.mark.skipif(shutil.which("pgrep") is None, reason="需要 pgrep(macOS 驗收平台)")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _group_alive(pgid: int) -> bool:
    r = subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")


class Serve:
    def __init__(self, home: Path, graceful: float = GRACEFUL) -> None:
        self.home = home
        self.fake = FakeCli(home, install(home / "fake-cli-bin"))
        base = {k: v for k, v in os.environ.items() if k not in REAL_KEY_VARS}
        base.update(SPILLWAY_HOME=str(home), PYTHONPATH=str(SRC), LITELLM_LOCAL_MODEL_COST_MAP="True")
        base[cli.TEST_GRACEFUL_TIMEOUT_ENV] = str(graceful)
        self.env = safe_subprocess_env(base, fake_bin=self.fake.bin_dir)
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        # 自成 session:對它的 process group 送訊號不會打到測試行程
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "spillway", "serve", "--port", str(self.port)],
            env=self.env, cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        self.out = self.err = ""

    def wait_ready(self) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self.out, self.err = self.proc.communicate()
                pytest.fail(f"serve 提前結束 rc={self.proc.returncode}\n{self.err}")
            try:
                if httpx.get(f"{self.url}/api/settings/context", timeout=2).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.2)
        pytest.fail("serve 未在 60 秒內就緒")

    def setup_member_and_route(self) -> None:
        r = httpx.post(f"{self.url}/api/deployments", json=CLAUDE_MEMBER, timeout=30)
        assert r.status_code == 201, r.text
        r = httpx.post(f"{self.url}/api/routes", json=ROUTE, timeout=30)
        assert r.status_code == 201, r.text

    def start_task_request(self) -> dict[str, Any]:
        box: dict[str, Any] = {}

        def go() -> None:
            try:
                box["resp"] = httpx.post(f"{self.url}/v1/chat/completions", json=TASK_BODY, timeout=60)
            except httpx.HTTPError as exc:
                box["exc"] = exc

        t = threading.Thread(target=go, daemon=True)
        t.start()
        box["thread"] = t
        return box

    def wait_exit(self, limit: float) -> float:
        start = time.monotonic()
        try:
            self.out, self.err = self.proc.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            pytest.fail(f"serve 未在 {limit} 秒內結束")
        return time.monotonic() - start

    def stop(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.communicate()

    def ledger(self) -> list[repo.LedgerRow]:
        conn = connect(self.home / "spillway.db")
        try:
            return repo.list_ledger(conn)
        finally:
            conn.close()


def _open_serve(home: Path, graceful: float):
    home.mkdir()
    s = Serve(home, graceful)
    try:
        s.wait_ready()
        s.setup_member_and_route()
        s.fake.set("claude", "hold_until_release")
        yield s
    finally:
        s.stop()
        # 保險:殺掉任何殘留的假 claude group,再刪控制目錄(假 CLI 偵測到也會自行結束)
        for rec in s.fake.records("claude"):
            if _group_alive(rec["pid"]):
                try:
                    os.killpg(rec["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
        shutil.rmtree(s.fake.control_dir, ignore_errors=True)


@pytest.fixture
def serve(tmp_path):
    yield from _open_serve(tmp_path / "home", GRACEFUL)


@pytest.fixture
def slow_serve(tmp_path):
    """停機上限 20 秒:分得出 force_exit(立即)與 graceful(等滿上限)。"""
    yield from _open_serve(tmp_path / "home", SLOW_GRACEFUL)


def _wait_fake_started(s: Serve) -> dict[str, Any]:
    (rec,) = s.fake.wait_for_records("claude", 1, timeout=30)
    assert _pid_alive(rec["pid"])
    return rec


def _new_request_not_served(s: Serve) -> None:
    """停機期間新進請求:serve 已停止接新連線則連線失敗,否則必須是 503;都不可啟動新的假 claude。"""
    time.sleep(0.3)   # 讓 serve 處理完訊號(旗標在訊號處理器內同步設好)
    try:
        r = httpx.post(f"{s.url}/v1/chat/completions", json=TASK_BODY, timeout=10)
    except httpx.TransportError:
        pass
    else:
        assert r.status_code == 503, r.text
        assert "shutting_down" in r.text


def _assert_shutting_down_json(resp: httpx.Response) -> None:
    assert resp.status_code == 503, resp.text
    assert resp.headers["content-type"].startswith("application/json"), resp.headers
    body = resp.json()
    assert body["error"]["code"] == "shutting_down", body


def _assert_shutdown_outcome(s: Serve, rec: dict[str, Any], box: dict[str, Any]) -> None:
    assert not _group_alive(rec["pid"]), "假 claude 或其孫行程仍在"
    assert not _pid_alive(rec["pid"])
    assert len(s.fake.records("claude")) == 1                # 停機期間沒有再啟動假 claude
    rows = [r for r in s.ledger() if r.deployment_id == "claude-sub"]
    assert len(rows) == 1, rows
    assert rows[0].status == "error" and rows[0].error == "shutdown", rows[0]
    box["thread"].join(10)
    if "resp" in box:   # 進行中的請求在停機期滿時得到 503 JSON shutting_down(決策 18;不是 500 純文字)
        _assert_shutting_down_json(box["resp"])


@skip_without_pgrep
def test_sigterm_waits_graceful_then_kills_cli_group_and_records_shutdown(serve):
    s = serve
    box = s.start_task_request()
    rec = _wait_fake_started(s)

    signalled_at = time.monotonic()
    s.proc.send_signal(signal.SIGTERM)
    _new_request_not_served(s)
    s.wait_exit(GRACEFUL + TERM_GRACE + SLACK)
    elapsed = time.monotonic() - signalled_at
    assert elapsed >= cli.drain_expire_delay(GRACEFUL) - 0.5, f"沒有等待進行中請求({elapsed:.1f}s)"
    assert s.proc.returncode == -signal.SIGTERM, s.err
    _assert_shutdown_outcome(s, rec, box)


@skip_without_pgrep
def test_sigint_to_process_group_does_not_hit_cli_child_directly(serve):
    s = serve
    box = s.start_task_request()
    rec = _wait_fake_started(s)

    signalled_at = time.monotonic()
    os.killpg(s.proc.pid, signal.SIGINT)                     # 模擬終端 Ctrl-C(整個前景 group)
    time.sleep(0.5)
    assert _pid_alive(rec["pid"]), "假 claude 自成 session,不應被終端的 SIGINT 直接打到"
    _new_request_not_served(s)
    s.wait_exit(GRACEFUL + TERM_GRACE + SLACK)
    elapsed = time.monotonic() - signalled_at
    assert elapsed >= cli.drain_expire_delay(GRACEFUL) - 0.5, f"沒有等待進行中請求({elapsed:.1f}s)"
    assert s.proc.returncode == 130, s.err
    _assert_shutdown_outcome(s, rec, box)


FORCE_LIMIT = TERM_GRACE + SLACK   # force_exit 時的結束上限(遠小於 SLOW_GRACEFUL)


def _assert_forced_outcome(s: Serve, rec: dict[str, Any], box: dict[str, Any], elapsed: float) -> None:
    assert not _group_alive(rec["pid"]), "force_exit 後假 claude 或其孫行程仍在"
    assert not _pid_alive(rec["pid"])
    assert elapsed < FORCE_LIMIT, f"force_exit 沒有立即生效({elapsed:.1f}s)"
    assert s.proc.returncode != 0
    assert len(s.fake.records("claude")) == 1
    box["thread"].join(10)


@skip_without_pgrep
def test_double_sigint_force_exit_is_prompt_and_leaves_no_cli_process(slow_serve):
    s = slow_serve
    box = s.start_task_request()
    rec = _wait_fake_started(s)

    os.killpg(s.proc.pid, signal.SIGINT)
    time.sleep(0.3)
    start = time.monotonic()
    os.killpg(s.proc.pid, signal.SIGINT)                     # 第二次:uvicorn force_exit,略過 lifespan
    s.wait_exit(SLOW_GRACEFUL + SLACK)
    _assert_forced_outcome(s, rec, box, time.monotonic() - start)


@skip_without_pgrep
def test_sigterm_then_sigint_force_exit_is_prompt_and_leaves_no_cli_process(slow_serve):
    """SIGTERM 後再 SIGINT:uvicorn 依相反順序重送,最後是預設處置的 SIGTERM(行程立即結束,
    `finally`/`atexit` 不會跑);子行程必須在重送之前就被殺掉。"""
    s = slow_serve
    box = s.start_task_request()
    rec = _wait_fake_started(s)

    s.proc.send_signal(signal.SIGTERM)
    time.sleep(0.3)
    start = time.monotonic()
    s.proc.send_signal(signal.SIGINT)
    s.wait_exit(SLOW_GRACEFUL + SLACK)
    assert s.proc.returncode == -signal.SIGTERM, s.err
    _assert_forced_outcome(s, rec, box, time.monotonic() - start)


# ---- CHG-20261102-01 T7:停機期滿 503、drain helper、Ctrl-C(決策 14 ①②、18、19) ----------------
#
# L1(行程內):第一個訊號觸發 `shutdown_hooks.fire` 並以 `call_later(max(grace*0.5, grace-1.5))` 排定
# `drain_expired`;force_exit 的 `_cancel_inflight` 也取消 `drain_tasks`;`_run_server` 停用 sse-starlette
# 的 auto-drain;`main` 把 `_run_server` 外層的 KeyboardInterrupt 轉成 130。
# L1(子行程,寬限 2 秒):SIGTERM 後進行中請求在寬限內得到 503 JSON `shutting_down`(假 claude 忽略
# SIGTERM 時亦然,重複 5 次);對 process group 送 SIGINT → stderr 無 Traceback、結束碼 130。

def test_drain_expire_delay():
    assert cli.drain_expire_delay(2.0) == 1.0
    assert cli.drain_expire_delay(1.0) == 0.5
    assert cli.drain_expire_delay(30.0) == 28.5


def test_run_server_disables_sse_auto_drain(monkeypatch):
    from sse_starlette.sse import AppStatus

    monkeypatch.setattr(AppStatus, "enable_automatic_graceful_drain", True)
    monkeypatch.setattr(AppStatus, "should_exit", False)
    _capture_server(monkeypatch)
    with pytest.raises(_Captured):
        cli._run_server("127.0.0.1", 8787)
    assert AppStatus.enable_automatic_graceful_drain is False
    assert AppStatus.should_exit is False                    # 不可設定(行程全域、不會重置)


def test_main_turns_keyboard_interrupt_into_130(monkeypatch, capsys):
    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_run_server", interrupted)
    assert cli.main(["serve", "--port", "8787"]) == 130
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.anyio
async def test_first_signal_fires_hooks_and_schedules_drain_expired(monkeypatch):
    import asyncio

    seen = _capture_server(monkeypatch)
    with pytest.raises(_Captured):
        cli._run_server("127.0.0.1", 8787)
    server = seen["server"]
    app = server.config.app
    server._loop = asyncio.get_running_loop()
    server.config.timeout_graceful_shutdown = 0.4             # → drain_expired 於 0.2 秒後
    fired: list[str] = []
    app.state.shutdown_hooks.append(lambda: fired.append("hook"))

    start = time.monotonic()
    server.handle_exit(signal.SIGTERM, None)
    assert fired == []                                       # 不在訊號處理器內直接呼叫
    await asyncio.sleep(0.05)
    assert fired == ["hook"]
    assert not app.state.drain_expired.is_set()
    await asyncio.wait_for(app.state.drain_expired.wait(), 2)
    assert time.monotonic() - start == pytest.approx(0.2, abs=0.25)

    server.handle_exit(signal.SIGTERM, None)                 # 之後的訊號不重複觸發
    await asyncio.sleep(0.05)
    assert fired == ["hook"]


@pytest.mark.anyio
async def test_default_empty_hooks_first_signal_only_sets_flag(monkeypatch):
    import asyncio

    seen = _capture_server(monkeypatch)
    with pytest.raises(_Captured):
        cli._run_server("127.0.0.1", 8787)
    server = seen["server"]
    app = server.config.app
    server._loop = asyncio.get_running_loop()
    assert list(app.state.shutdown_hooks) == []
    server.handle_exit(signal.SIGTERM, None)
    await asyncio.sleep(0.05)
    assert app.state.shutting_down is True and server.should_exit and not server.force_exit
    assert app.state.shutdown_hooks.fired is True


@pytest.mark.anyio
async def test_force_exit_cancels_drain_tasks(monkeypatch):
    import asyncio

    seen = _capture_server(monkeypatch)
    with pytest.raises(_Captured):
        cli._run_server("127.0.0.1", 8787)
    server = seen["server"]
    app = server.config.app
    server._loop = asyncio.get_running_loop()
    tool_task = asyncio.ensure_future(asyncio.sleep(60))    # 例如 MCP 工具 task(不在 server_state.tasks)
    app.state.drain_tasks.add(tool_task)
    server.handle_exit(signal.SIGINT, None)
    await asyncio.sleep(0.05)
    assert not tool_task.done()
    server.handle_exit(signal.SIGINT, None)                  # force_exit
    await asyncio.wait({tool_task}, timeout=1)
    assert tool_task.cancelled()


def _start_timed_request(s: Serve) -> dict[str, Any]:
    box: dict[str, Any] = {}

    def go() -> None:
        try:
            box["resp"] = httpx.post(f"{s.url}/v1/chat/completions", json=TASK_BODY, timeout=60)
        except httpx.HTTPError as exc:
            box["exc"] = exc
        box["at"] = time.monotonic()

    t = threading.Thread(target=go, daemon=True)
    t.start()
    box["thread"] = t
    return box


def _make_fake_claude_ignore_sigterm(s: Serve) -> None:
    """把假 `claude` 包一層 `trap '' TERM`:SIG_IGN 跨 exec 繼承,假 CLI 收到 SIGTERM 不會結束。"""
    from tests.fakes.cli_upstream.harness import FAKE_MARKER, FAKE_SCRIPT
    import shlex

    target = s.fake.bin_dir / "claude"
    target.write_text(
        "#!/bin/sh\n"
        f"{FAKE_MARKER}\n"
        "trap '' TERM\n"
        f"exec {shlex.quote(sys.executable)} -I {shlex.quote(str(FAKE_SCRIPT))} claude \"$@\"\n",
        encoding="utf-8",
    )
    target.chmod(0o755)


def _assert_503_within_grace(s: Serve, box: dict[str, Any], signalled_at: float) -> None:
    box["thread"].join(GRACEFUL + TERM_GRACE + SLACK)
    assert "resp" in box, box.get("exc")
    _assert_shutting_down_json(box["resp"])
    assert box["at"] - signalled_at < GRACEFUL, f"503 晚於寬限({box['at'] - signalled_at:.2f}s)"


@skip_without_pgrep
def test_sigterm_inflight_request_gets_503_json_and_ledger_shutdown(serve):
    s = serve
    box = _start_timed_request(s)
    rec = _wait_fake_started(s)

    signalled_at = time.monotonic()
    s.proc.send_signal(signal.SIGTERM)
    _assert_503_within_grace(s, box, signalled_at)
    s.wait_exit(GRACEFUL + TERM_GRACE + SLACK)
    assert s.proc.returncode == -signal.SIGTERM, s.err
    _assert_shutdown_outcome(s, rec, box)


@skip_without_pgrep
@pytest.mark.parametrize("run", range(5))
def test_sigterm_ignoring_fake_claude_still_gets_503_within_grace(tmp_path, run):
    home = tmp_path / "home"
    gen = _open_serve(home, GRACEFUL)
    s = next(gen)
    try:
        _make_fake_claude_ignore_sigterm(s)
        box = _start_timed_request(s)
        rec = _wait_fake_started(s)

        signalled_at = time.monotonic()
        s.proc.send_signal(signal.SIGTERM)
        _assert_503_within_grace(s, box, signalled_at)
        s.wait_exit(GRACEFUL + TERM_GRACE + SLACK)
        assert s.proc.returncode == -signal.SIGTERM, s.err
        _assert_shutdown_outcome(s, rec, box)
    finally:
        gen.close()


@skip_without_pgrep
def test_ctrl_c_to_process_group_exits_130_without_traceback(serve):
    s = serve
    box = _start_timed_request(s)
    rec = _wait_fake_started(s)

    signalled_at = time.monotonic()
    os.killpg(s.proc.pid, signal.SIGINT)                     # 模擬終端 Ctrl-C
    _assert_503_within_grace(s, box, signalled_at)
    s.wait_exit(GRACEFUL + TERM_GRACE + SLACK)
    assert s.proc.returncode == 130, s.err
    assert "Traceback" not in s.err, s.err
    assert "KeyboardInterrupt" not in s.err, s.err
    _assert_shutdown_outcome(s, rec, box)


@pytest.mark.anyio
@skip_without_pgrep
async def test_pre_lifespan_wait_covers_lingering_drain_tasks(monkeypatch):
    """`run_until_drain` 回 Drained 後仍在收尾的子 task(不在 server_state.tasks)在 lifespan 結束前被等到:
    CliRunner 走完 SIGTERM→寬限→SIGKILL,group 不殘留,之後才跑 lifespan shutdown。"""
    import asyncio
    from types import SimpleNamespace

    from spillway.adapters.base import CliRunner

    seen = _capture_server(monkeypatch)
    with pytest.raises(_Captured):
        cli._run_server("127.0.0.1", 8787)
    server = seen["server"]
    app = server.config.app
    runner = CliRunner(term_grace=0.5)
    pids: list[int] = []
    orig_exec = asyncio.create_subprocess_exec

    async def spy_exec(*a, **k):
        proc = await orig_exec(*a, **k)
        pids.append(proc.pid)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy_exec)
    child = asyncio.ensure_future(
        runner.run(["/bin/sh", "-c", "trap '' TERM; sleep 30 & wait"], cwd=None, timeout=None))
    app.state.drain_tasks.add(child)
    child.add_done_callback(app.state.drain_tasks.discard)
    for _ in range(100):
        if pids:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.1)
    child.cancel()                                           # 第一次 cancel:shielded 收尾(SIGTERM 被忽略)
    order: list[str] = []

    async def lifespan_shutdown() -> None:
        order.append("lifespan" + ("" if child.done() else " before child done"))

    server.lifespan = SimpleNamespace(shutdown=lifespan_shutdown)
    server.servers = []
    await asyncio.wait_for(server.shutdown(), cli.CANCELLED_DRAIN_TIMEOUT + 5)
    assert order == ["lifespan"]
    assert child.done()
    assert not _group_alive(pids[0]), "收尾後 group 仍在"

"""黑箱 harness 與共用零件的測試(CHG-20261107-03 T2;契約清單「harness `Serve`/`Control`/`FakeApi`/`read_db`
與共用零件」)。

L1:`Serve` 啟動、就緒、token 只從 `TOKEN_NOTICE` 解析、`restart` 延續時鐘、`stop()` 的結束碼/Traceback/
ERROR/逾時斷言、日誌遮 token、`assert_only_fakes` 檢查子行程 PATH;`SPILLWAY_CMD` 覆寫生效且 `init`、
`mcp-stdio` 也經它;`read_db` 唯讀;`adapter_paths` 的載入斷言(T1 審議 A5);共用零件(UI 種資料、遮罩、
價格表、執行期守門)。
L2:harness 以假上游完成一次 `/v1/chat/completions` 往返;基準清單的數量(acceptance 20、ui 190、behave 60)。

只經 harness(HTTP、子行程、唯讀資料庫);不 import spillway。
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
import tomllib
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.blackbox import guard, harness, plugin
from tests.blackbox.control import iso
from tests.blackbox.db import read_db, snapshot
from tests.blackbox.fakemods import write_module
from tests.blackbox.harness import (
    EXIT_USAGE, TOKEN_NOTICE, Serve, child_env, parse_tokens, run_cli, spillway_cmd, stderr_problems,
)
from tests.blackbox.mask import MASK, TEST_KEY, TEST_KEY_ENV, mask_secrets, redact_tokens
from tests.blackbox.mcp import stdio_session
from tests.blackbox.prices import cost_per_token, expected_cost, prices_path
from tests.blackbox.ui import ledger as ui_ledger
from tests.blackbox.ui import overview as ui_overview
from tests.blackbox.ui import routes as ui_routes
from tests.blackbox.ui import settings as ui_settings
from tests.fakes.cli_upstream import RealCliFound

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE = REPO_ROOT / "artifacts" / "m5a" / "baseline"
START = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
API_MEMBER = {
    "id": "api-a", "model_name": "api-a", "kind": "api", "provider": "openai", "model": "gpt-4o-mini",
    "credential_ref": f"env:{TEST_KEY_ENV}",
    "capabilities": {"ctx": 128000, "tools": True, "vision": False, "efforts": []}, "tags": ["chat"],
}


def _member(fake_api, **kw):
    return {**API_MEMBER, "params": {"api_base": fake_api.base_url + "/v1"}, **kw}


# ================================================================ SPILLWAY_CMD


def test_spillway_cmd_default_and_override(monkeypatch):
    monkeypatch.delenv("SPILLWAY_CMD", raising=False)
    assert spillway_cmd() == [sys.executable, "-m", "spillway"]
    monkeypatch.setenv("SPILLWAY_CMD", "  ")
    assert spillway_cmd() == [sys.executable, "-m", "spillway"]
    monkeypatch.setenv("SPILLWAY_CMD", "/opt/x/spillway-rs --flag 'a b'")
    assert spillway_cmd() == ["/opt/x/spillway-rs", "--flag", "a b"]


def _py_wrapper(tmp_path: Path, name: str, body: str) -> str:
    """`SPILLWAY_CMD` 用的 Python 包裝程式:`IMPL` 為**目前的** `spillway_cmd()`(換實作時包的是該實作,
    施工審議 T2 F6),`ARGS` 為子命令參數;回傳可設給 `SPILLWAY_CMD` 的字串。必須在設定 `SPILLWAY_CMD` 前呼叫。"""
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import json, os, signal, subprocess, sys, time\n"
        f"IMPL = json.loads({json.dumps(json.dumps(spillway_cmd()))})\n"
        "ARGS = sys.argv[1:]\n" + body, encoding="utf-8")
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"


def _recording_cmd(tmp_path: Path) -> tuple[str, Path]:
    """把子命令參數記到檔案,再 exec 實作。"""
    log = tmp_path / "cmd-log.jsonl"
    cmd = _py_wrapper(tmp_path, "rec", f"with open({str(log)!r}, 'a', encoding='utf-8') as f:\n"
                                       "    f.write(json.dumps(ARGS) + '\\n')\n"
                                       "os.execvp(IMPL[0], IMPL + ARGS)\n")
    return cmd, log


def test_serve_init_and_mcp_stdio_all_go_through_spillway_cmd(tmp_path, monkeypatch):
    cmd, log = _recording_cmd(tmp_path)
    monkeypatch.setenv("SPILLWAY_CMD", cmd)
    home = tmp_path / "home"
    r = run_cli("init", home=home)
    assert r.returncode == 0, r.stderr
    token = r.stdout.strip()
    assert len(token) == 43 and token not in r.stderr
    with Serve(home) as s:
        assert s.token is None   # init 已產生 token:serve 不再印 TOKEN_NOTICE
        out = asyncio.run(stdio_session(group="all", port=s.port, token=token, bridge_home=tmp_path / "bridge"))
        assert out.error is None and out.init is not None, out.stderr[-2000:]
    calls = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]
    assert [c[0] for c in calls] == ["init", "serve", "mcp-stdio"]
    assert calls[1] == ["serve", "--port", str(s.port)]
    assert calls[2][calls[2].index("--port") + 1] == str(s.port)


# ================================================================ Serve:啟動、就緒、token


def test_parse_tokens_only_whole_token_notice_lines():
    tok = "A" * 21 + "-_" + "z9" * 10
    assert len(tok) == 43
    notice = TOKEN_NOTICE.encode()
    data = b"\n".join([
        notice + tok.encode(),
        b"x" + notice + b"B" * 43,             # 前面多了字元
        notice + b"C" * 42,                    # 長度不對
        notice + b"D" * 43 + b" trailing",     # 後面多了字元
        b"token: " + b"E" * 43,                # 別的行
    ]) + b"\n"
    assert parse_tokens(data) == [tok]


def test_serve_ready_token_and_log_masking(serve_factory):
    s = serve_factory(start_time=START)
    assert s.returncode is None
    assert s.request("GET", "/api/settings/context").status_code == 200
    assert s.token is not None and len(s.token) == 43
    # 解析到的 token 就是 serve 簽發的那一個:/mcp 接受它、拒絕別的
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    accept = {"Accept": "application/json, text/event-stream"}
    assert s.request("POST", "/mcp/all", json=init, headers={**accept, **s.auth_headers()}).status_code == 200
    assert s.request("POST", "/mcp/all", json=init, headers={**accept, **s.auth_headers("x" * 43)}).status_code == 401
    assert s.token.encode() in s.stderr_bytes()            # 原始 stderr 留在記憶體
    s.stop()
    saved = s.log_path.read_text(encoding="utf-8")
    assert s.token not in saved and TOKEN_NOTICE + "***" in saved   # 保存的日誌遮掉 token
    assert s.token not in s.stderr_text()


_FAKE_TOKEN = "F" * 20 + "-_" + "k7" * 10 + "Q"
# 不 exec 實作的假 serve:啟動時在 stderr 印 TOKEN_NOTICE,`GET /emit` 時把哨兵字串與該 token 寫到 stdout
_STDOUT_EMITTER = (
    "import http.server\n"
    f"TOKEN = {_FAKE_TOKEN!r}\n"
    f"sys.stderr.write({TOKEN_NOTICE!r} + TOKEN + '\\n')\n"
    "sys.stderr.flush()\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        if self.path == '/emit':\n"
    "            sys.stdout.write('SENTINEL-STDOUT-7f3a token=' + TOKEN + '\\n')\n"
    "            sys.stdout.flush()\n"
    "        self.send_response(200)\n"
    "        self.send_header('Content-Length', '0')\n"
    "        self.end_headers()\n"
    "    def log_message(self, *a):\n"
    "        pass\n"
    "http.server.HTTPServer(('127.0.0.1', int(ARGS[ARGS.index('--port') + 1])), H).serve_forever()\n")


def _wait_for(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_stdout_is_captured_and_masked_in_log(tmp_path, monkeypatch):
    """施工審議 T3 F1:serve 的 stdout 由第二個讀取執行緒保留原始位元組(`stdout_bytes()`),
    寫入 `log_path` 的副本把 token 換成 `***`。"""
    monkeypatch.setenv("SPILLWAY_CMD", _py_wrapper(tmp_path, "emitter", _STDOUT_EMITTER))
    s = Serve(tmp_path / "home", control=False).start(timeout=10)
    try:
        assert s.token == _FAKE_TOKEN
        assert s.request("GET", "/emit").status_code == 200
        assert _wait_for(lambda: b"SENTINEL-STDOUT-7f3a" in s.stdout_bytes())
        assert _FAKE_TOKEN.encode() in s.stdout_bytes()        # 原始 stdout 未遮
        assert _FAKE_TOKEN not in s.stdout_text() and "token=***" in s.stdout_text()
    except BaseException:
        s.kill()
        raise
    s.stop()
    saved = s.log_path.read_text(encoding="utf-8")
    assert "SENTINEL-STDOUT-7f3a token=***" in saved   # stdout 也寫進日誌副本,token 已遮
    assert _FAKE_TOKEN not in saved and TOKEN_NOTICE + "***" in saved


def test_real_serve_access_log_lands_in_stdout(serve_factory):
    """施工審議 T3 F1:uvicorn access log(含查詢字串)寫在 serve stdout,所以 token 外洩掃描必須涵蓋
    `stdout_bytes()`;保存的日誌副本與 `stdout_text()` 遮掉 token。"""
    s = serve_factory()
    assert s.token is not None
    assert s.request("GET", "/api/settings/context", params={"probe": s.token}).status_code == 200
    assert _wait_for(lambda: s.token.encode() in s.stdout_bytes()), s.stdout_text()[-2000:]
    assert s.token not in s.stdout_text() and "probe=***" in s.stdout_text()
    s.stop()
    saved = s.log_path.read_text(encoding="utf-8")
    assert "probe=***" in saved and s.token not in saved


def test_free_port_never_returns_default_serve_port(monkeypatch):
    """施工審議 T4 F6:作業系統剛好配到 8787 時重抽。"""
    from tests.blackbox import ports

    seq = iter([ports.DEFAULT_SERVE_PORT, ports.DEFAULT_SERVE_PORT, 40123])
    monkeypatch.setattr(ports, "_os_free_port", lambda: next(seq))
    assert ports.free_port() == 40123


def test_no_token_knob_leaves_token_none(serve_factory):
    s = serve_factory(no_token=True)
    assert s.token is None
    assert s.request("GET", "/api/settings").json()["token_issued"] is False


def test_restart_continues_clock_and_reseeds(serve_factory):
    s = serve_factory(start_time=START, rng_seed=99)
    token = s.token
    s.control.advance(3600)
    s.restart()
    assert s.control.clock() == {"controllable": True, "now": iso(START + timedelta(hours=1))}
    assert s.control.rng() == {"seeded": True, "seed": 99}
    assert s.token == token   # 同一資料庫:不再簽發
    s.restart(rng_seed=None)
    assert s.control.rng() == {"seeded": False, "seed": None}
    with pytest.raises(TypeError):
        s.restart(no_such_option=1)


def test_start_reports_early_exit(tmp_path):
    s = Serve(tmp_path / "home", log_level="TRACE")   # 旋鈕值不合法 → 結束碼 2
    with pytest.raises(RuntimeError, match=f"rc={EXIT_USAGE}"):
        s.start()
    assert s.returncode == EXIT_USAGE


def test_home_must_be_under_tempdir():
    with pytest.raises(RuntimeError, match="暫存目錄"):
        Serve(Path.home() / "spillway-not-tmp")
    with pytest.raises(RuntimeError, match="暫存目錄"):
        child_env(Path.home() / "spillway-not-tmp")


# ================================================================ Serve:stop() 的斷言


def test_stop_asserts_exit_code(tmp_path):
    s = Serve(tmp_path / "home").start()
    with pytest.raises(AssertionError, match="結束碼 -15,預期 0"):
        s.stop(expect_code=0)
    assert s.returncode == -signal.SIGTERM
    assert s.stop() == -signal.SIGTERM   # 冪等


def test_stop_sigint_is_130(serve_factory):
    s = serve_factory()
    assert s.stop(signal.SIGINT) == 130


def test_stop_asserts_timeout(tmp_path, monkeypatch):
    """包裝程式忽略 SIGTERM 且在實作結束後不退出:stop() 逾時 → SIGKILL 並失敗。"""
    cmd = _py_wrapper(tmp_path, "stubborn",
                      "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                      "p = subprocess.Popen(IMPL + ARGS, preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_DFL))\n"
                      "p.wait()\n"
                      "time.sleep(600)\n")
    monkeypatch.setenv("SPILLWAY_CMD", cmd)
    s = Serve(tmp_path / "home").start()
    with pytest.raises(AssertionError, match="未在 3.0 秒內結束"):
        s.stop(timeout=3.0)
    assert s.returncode == -signal.SIGKILL
    assert not harness._group_alive(s.proc.pid)


# 施工審議 T2 F4:stop() 檢查 stderr EOF 與行程群組;包裝程式的轉送與結束狀態(harness docstring 的要求)
# 孫行程忽略 SIGTERM/SIGINT(送到群組的停機訊號殺不掉它),模擬實作或包裝程式留下的孤兒
_IGNORE = "def _ign():\n    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n    signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
_GRANDCHILD_HOLDS_STDERR = (_IGNORE + "subprocess.Popen(['sleep', '60'], stdin=subprocess.DEVNULL, "
                            "stdout=subprocess.DEVNULL, preexec_fn=_ign)\n"
                            "os.execvp(IMPL[0], IMPL + ARGS)\n")
_GRANDCHILD_CLOSES_STDERR = (_IGNORE + "subprocess.Popen(['sleep', '60'], stdin=subprocess.DEVNULL, "
                             "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=_ign)\n"
                             "os.execvp(IMPL[0], IMPL + ARGS)\n")
_FORWARD = ("child = subprocess.Popen(IMPL + ARGS, start_new_session=True)\n"
            "def fwd(sig, frame):\n"
            "    try:\n"
            "        os.killpg(child.pid, sig)\n"
            "    except ProcessLookupError:\n"
            "        pass\n"
            "signal.signal(signal.SIGTERM, fwd)\n"
            "signal.signal(signal.SIGINT, fwd)\n"
            "rc = child.wait()\n")
_REEMIT = ("if rc < 0:\n"
           "    signal.signal(-rc, signal.SIG_DFL)\n"
           "    os.kill(os.getpid(), -rc)\n"
           "    time.sleep(5)\n"
           "sys.exit(rc)\n")


def test_stop_fails_when_grandchild_holds_stderr(tmp_path, monkeypatch):
    monkeypatch.setenv("SPILLWAY_CMD", _py_wrapper(tmp_path, "holds", _GRANDCHILD_HOLDS_STDERR))
    s = Serve(tmp_path / "home").start()
    with pytest.raises(AssertionError, match="stderr 未關閉"):
        s.stop()
    assert not harness._group_alive(s.proc.pid)   # 殘留已被 SIGKILL


def test_stop_fails_when_process_group_not_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("SPILLWAY_CMD", _py_wrapper(tmp_path, "lingers", _GRANDCHILD_CLOSES_STDERR))
    s = Serve(tmp_path / "home").start()
    with pytest.raises(AssertionError, match="行程群組仍有行程殘留"):
        s.stop()
    assert not harness._group_alive(s.proc.pid)


@pytest.mark.parametrize("sig,code", [(signal.SIGTERM, -15), (signal.SIGINT, 130)])
def test_stop_accepts_forwarding_wrapper_that_reemits_signal(tmp_path, monkeypatch, sig, code):
    monkeypatch.setenv("SPILLWAY_CMD", _py_wrapper(tmp_path, "fwd", _FORWARD + _REEMIT))
    s = Serve(tmp_path / "home").start()
    assert s.stop(sig) == code


def test_stop_rejects_forwarding_wrapper_that_exits_143(tmp_path, monkeypatch):
    monkeypatch.setenv("SPILLWAY_CMD", _py_wrapper(tmp_path, "fwd143",
                                                   _FORWARD + "sys.exit(128 - rc if rc < 0 else rc)\n"))
    s = Serve(tmp_path / "home").start()
    with pytest.raises(AssertionError, match="結束碼 143,預期 -15"):
        s.stop()


def test_relative_spillway_cmd_resolved_from_invocation_dir(tmp_path, monkeypatch):
    """`SPILLWAY_CMD=./wrap.sh`:以測試行程啟動時的目錄解析(子行程的 cwd 是暫存目錄;F5)。"""
    rel = tmp_path / "proj"
    rel.mkdir()
    wrap = rel / "wrap.sh"
    wrap.write_text("#!/bin/sh\nexec " + " ".join(shlex.quote(a) for a in spillway_cmd()) + ' "$@"\n',
                    encoding="utf-8")
    wrap.chmod(0o755)
    monkeypatch.chdir(rel)
    monkeypatch.setattr(harness, "INVOCATION_DIR", rel)
    monkeypatch.setenv("SPILLWAY_CMD", "./wrap.sh")
    assert spillway_cmd() == [str(wrap.resolve())]
    home = tmp_path / "home"
    assert run_cli("init", home=home).returncode == 0
    with Serve(home) as s:
        assert s.request("GET", "/api/settings/context").status_code == 200
    monkeypatch.setenv("SPILLWAY_CMD", "spillway-rs --x")   # 不含路徑分隔字元:照 PATH 查找,不解析
    assert spillway_cmd() == ["spillway-rs", "--x"]


def test_kill_live_registry(tmp_path):
    s = Serve(tmp_path / "home").start()
    assert s in harness._LIVE
    harness._kill_live()
    s.proc.wait(15)
    assert s.returncode == -signal.SIGKILL and s not in harness._LIVE
    s.kill()


def test_ready_waits_for_control_warning_line(tmp_path, monkeypatch):
    """開啟控制介面時,埠上的伺服器回 200 也不算就緒:必須先看到 14.3.3 的警告行(F8)。
    `SPILLWAY_CMD` 換成一個只會在指定埠回 200、不印警告行的程式。"""
    monkeypatch.setenv("SPILLWAY_CMD", _py_wrapper(tmp_path, "imposter", (
        "import http.server\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        "    def log_message(self, *a):\n"
        "        pass\n"
        "http.server.HTTPServer(('127.0.0.1', int(ARGS[ARGS.index('--port') + 1])), H).serve_forever()\n")))
    s = Serve(tmp_path / "home")
    with pytest.raises(RuntimeError, match="未在 3 秒內就緒"):
        s.start(timeout=3)
    assert s.returncode == -signal.SIGKILL
    s_off = Serve(tmp_path / "home2", control=False)   # 未開啟控制介面時不要求警告行
    s_off.start(timeout=10)
    s_off.kill()


def test_stderr_problems():
    text = "\n".join([
        "INFO-ish line",
        "Traceback (most recent call last):",
        "ERROR:    Exception in ASGI application",
        "SPILLWAY-LOG ERROR spillway.app 保存清理失敗",
        "SPILLWAY-LOG CRITICAL x y",
        "SPILLWAY-LOG WARNING x y",
        "spillway: 警告:測試控制介面已開啟",
        "ERRORS are fine mid-line? no: ERROR at start only",
    ])
    assert stderr_problems(text) == [
        "Traceback (most recent call last):", "ERROR:    Exception in ASGI application",
        "SPILLWAY-LOG ERROR spillway.app 保存清理失敗", "SPILLWAY-LOG CRITICAL x y"]
    assert stderr_problems(text, allow=[r"ASGI", r"保存清理", r"CRITICAL", r"Traceback"]) == []


def test_kill_on_failure_has_no_assertions(tmp_path):
    s = Serve(tmp_path / "home").start()
    s.kill()
    assert s.returncode == -signal.SIGKILL


# ================================================================ 子行程 PATH、唯讀資料庫


def _real_looking_cli(d: Path) -> Path:
    d.mkdir()
    exe = d / "claude"
    exe.write_text("#!/bin/sh\necho real\n", encoding="utf-8")
    exe.chmod(0o755)
    return d


def test_child_env_checks_subprocess_path(tmp_path, monkeypatch):
    bad = _real_looking_cli(tmp_path / "realbin")
    with pytest.raises(RealCliFound):
        child_env(tmp_path / "home", fake_bin=bad)
    env = child_env(tmp_path / "home", extra={"PATH": os.pathsep.join([str(bad), os.environ["PATH"]])})
    assert str(bad) not in env["PATH"].split(os.pathsep)   # 清理掉含真實 CLI 的目錄
    for name in ("OPENAI_API_KEY", "SPILLWAY_TOKEN", "SPILLWAY_TEST_CONTROL", "SPILLWAY_TEST_RNG_SEED"):
        monkeypatch.setenv(name, "x")
    env = child_env(tmp_path / "home")
    assert not {"OPENAI_API_KEY", "SPILLWAY_TOKEN", "SPILLWAY_TEST_CONTROL", "SPILLWAY_TEST_RNG_SEED"} & set(env)
    assert env["SPILLWAY_HOME"] == str((tmp_path / "home").resolve())


def test_read_db_is_read_only(serve_factory):
    s = serve_factory()
    with s.db() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO settings (key, value) VALUES ('x', 'y')")
    s.stop()
    with read_db(s.home) as conn:   # 停機後也可唯讀開啟
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM settings")
    with pytest.raises(FileNotFoundError):
        with read_db(s.home / "nope"):
            pass


# ================================================================ adapter_paths 的載入斷言(T1 審議 A5)


def test_adapter_paths_are_verified_loaded(tmp_path, serve_factory):
    with pytest.raises(AssertionError, match="沒有任何模組資料夾"):
        serve_factory(adapter_paths=[tmp_path / "does-not-exist"])
    mods = tmp_path / "mods"
    write_module(mods, "bbprobe", providers=("bbprobe",))
    s = serve_factory(adapter_paths=[mods])
    providers = {p for m in s.request("GET", "/api/adapters").json()["modules"] for p in m["providers"]}
    assert "bbprobe" in providers
    broken = tmp_path / "broken"
    write_module(broken, "broken", pre_import="import not_a_real_pkg_xyz\n", providers=("broken-p",))
    with pytest.raises(AssertionError, match="載入失敗"):
        serve_factory(adapter_paths=[broken])
    serve_factory(adapter_paths=[broken], allowed_unavailable_adapters={"broken"})


# ================================================================ L2:/v1 往返


@pytest.mark.usefixtures("fake_key_env")
def test_chat_completion_round_trip_with_fake_upstream(serve_factory, fake_api):
    s = serve_factory(start_time=START)
    assert s.request("POST", "/api/deployments", json=_member(fake_api)).status_code == 201
    assert s.request("POST", "/api/routes", json={"name": "r", "target": {"tags": {"any": ["chat"]}}}).status_code == 201
    r = s.request("POST", "/v1/chat/completions", json={"model": "r", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    assert r.headers["X-Spillway-Served-By"] == "api-a"
    assert r.json()["choices"][0]["message"]["content"]
    reqs = fake_api.requests
    assert len(reqs) == 1 and reqs[0].has_auth
    with s.db() as conn:
        rows = conn.execute("SELECT deployment_id, status, tokens_in, tokens_out FROM usage_ledger").fetchall()
    assert rows == [("api-a", "ok", 11, 7)]


# ================================================================ MCP(HTTP)


def test_mcp_session_and_stream_open(serve_factory):
    s = serve_factory()

    async def body() -> None:
        async with s.mcp_session() as client:
            assert client.session_id
            assert client.init.serverInfo.name
            await s.wait_stream_open(client.session_id)   # SDK 用戶端自己開 GET 串流
            assert s.stream_open(client.session_id) is True

    asyncio.run(body())
    assert s.stream_open("no-such-session") is False


# ================================================================ UI 種資料零件(tests/blackbox/ui)


@pytest.mark.usefixtures("fake_key_env")
def test_ui_seed_helpers_go_through_control(serve_factory, fake_api, monkeypatch):
    s = serve_factory(start_time=START)
    assert s.request("POST", "/api/deployments", json=_member(fake_api)).status_code == 201
    ts = START - timedelta(hours=1)
    assert ui_ledger.insert_ledger(s, 7, ts, deployment="api-a", tokens_in=3, error="e") == 7
    ui_ledger.insert_payload(s, 7, ts, '{"a":1}', "out", truncated=True)
    monkeypatch.setattr(ui_ledger, "BULK_CHUNK", 10)
    ui_ledger.bulk_ledger(s, 25, ts, deployment="bulk")
    ui_overview.set_cooldown(s, "api-a", START + timedelta(hours=2), "rate_limited")
    assert ui_routes.set_cooldown(s, "api-a", START + timedelta(hours=3)) == "2026-11-01T15:00:00Z"
    with s.db() as conn:
        snap = snapshot(conn)
        assert conn.execute("SELECT route_name, deployment_id, status, tokens_in, fallback_depth, error "
                            "FROM usage_ledger WHERE id = 7").fetchone() == ("rt", "api-a", "ok", 3, 0, "e")
    assert len([r for r in snap["usage_ledger"] if r[2] == "bulk" or "bulk" in r]) == 25
    assert snap["usage_payloads"] == [(7, "2026-11-01T11:00:00Z", '{"a":1}', "out", 10, 1, 0)]
    assert snap["cooldowns"] == [("api-a", "2026-11-01T15:00:00Z", "rate_limited")]
    assert ui_settings.token_hash(s) == hashlib.sha256(s.token.encode()).hexdigest()


# ================================================================ 遮罩、價格表


def test_mask_helpers():
    assert TEST_KEY == "sk-test-0000" and TEST_KEY_ENV == "SPILLWAY_TEST_KEY"
    text = 'key sk-abcd1234 and Bearer tok.en-1 and task-1234 and secret-value'
    assert mask_secrets(text, ["secret-value", "", "ab"]) == f"key {MASK} and Bearer {MASK} and task-1234 and {MASK}"
    assert redact_tokens("a TOKEN b TOKENX", ["TOKEN", "TOKENX", None]) == "a <token> b <token>"


def test_prices_are_read_from_litellm_json_without_import(monkeypatch):
    for name in [m for m in sys.modules if m == "litellm" or m.startswith("litellm.")]:
        monkeypatch.delitem(sys.modules, name)   # 與 import litellm 的測試同一 session 時也成立
    assert prices_path().is_file()
    cin, cout = cost_per_token("openai/gpt-4o-mini")
    assert cin > 0 and cout > 0
    assert expected_cost("gpt-4o-mini", 11, 7) == pytest.approx(cin * 11 + cout * 7)
    assert "litellm" not in sys.modules


# ================================================================ 執行期守門


def test_runtime_guard_detects_spillway_modules():
    m = types.ModuleType("x")
    mods = {"spillwayx": m, "json": m, "tests.blackbox": m}
    guard.assert_no_spillway_modules(modules=mods)
    mods.update({"spillway": m, "spillway.app": m})
    assert guard.spillway_modules(mods) == ["spillway", "spillway.app"]
    with pytest.raises(AssertionError, match="spillway.app"):
        guard.assert_no_spillway_modules("test", modules=mods)


GUARD_CASES = {
    "clean": ("def test_x():\n    pass\n", None),
    "import": ("def test_x():\n    import spillway  # noqa\n", "黑箱邊界"),
    "runpy": ("import runpy\n\ndef test_x():\n    runpy.run_module('spillway.cli')\n", "黑箱邊界"),
    "import_module_var": ("import importlib\nNAME = 'spill' + 'way.app'\n\n"
                          "def test_x():\n    importlib.import_module(NAME)\n", "黑箱邊界"),
    "root_clock_fixture": ("def test_x(clock):\n    pass\n", "黑箱邊界"),
    # litellm 不經阻擋器:只有 autouse 的 session 守門會發現(F1 的變異「拿掉 autouse」必須讓這裡轉紅)
    "litellm": ("def test_x():\n    import litellm  # noqa\n", "行程內載入了 litellm"),
}


@pytest.mark.parametrize("case", sorted(GUARD_CASES))
def test_runtime_guard_in_forced_session(tmp_path, case):
    """子行程 pytest(只載入根 conftest 與 plugin,測試檔所在目錄**沒有** conftest),`SPILLWAY_BLACKBOX_GUARD=1`:
    autouse 的 session 守門與 import 阻擋器自動生效(施工審議 T2 F1、F2)。"""
    body, expect = GUARD_CASES[case]
    root = tmp_path / "converted"
    root.mkdir()
    (root / "test_guarded.py").write_text(body, encoding="utf-8")
    env = {**os.environ, "SPILLWAY_BLACKBOX_GUARD": "1", "LITELLM_LOCAL_MODEL_COST_MAP": "True",
           "PYTHONPATH": os.pathsep.join([str(REPO_ROOT), os.environ.get("PYTHONPATH", "")])}
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "tests.conftest", "-p", "no:cacheprovider",
                        str(root)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180)
    if expect is None:
        assert r.returncode == 0, r.stdout[-3000:]
    else:
        assert r.returncode != 0 and expect in r.stdout, r.stdout[-3000:]


def test_guard_roots_and_purity(tmp_path):
    f = tmp_path / "roots.txt"
    f.write_text("# 註解\n\ntests/acceptance/\ntests/blackbox\n  tests/ui\n", encoding="utf-8")
    assert guard.converted_roots(f) == ["tests/blackbox", "tests/acceptance", "tests/ui"]
    assert guard.converted_roots()[0] == "tests/blackbox"
    roots = ["tests/blackbox", "tests/ui"]
    assert plugin._under(REPO_ROOT / "tests/blackbox/test_harness.py", roots)
    assert plugin._under(REPO_ROOT / "tests/ui/test_x.py", roots)
    assert not plugin._under(REPO_ROOT / "tests/uix/test_x.py", roots)
    assert not plugin._under(REPO_ROOT / "tests/unit/test_x.py", roots)


# ================================================================ 基準清單與實作相依清單


def test_baseline_counts_from_7fadd84():
    summary = json.loads((BASELINE / "summary.json").read_text(encoding="utf-8"))
    assert summary["commit"].startswith("7fadd84")
    assert summary["pytest_nodeids"] == {"tests/acceptance": 20, "tests/ui": 190}
    assert summary["behave_features"] == 10 and summary["behave_scenarios"] == 60
    nodeids = json.loads((BASELINE / "pytest_nodeids.json").read_text(encoding="utf-8"))
    assert {k: len(v) for k, v in nodeids.items()} == summary["pytest_nodeids"]
    scenarios = json.loads((BASELINE / "behave_scenarios.json").read_text(encoding="utf-8"))
    assert len(scenarios) == 60 and sum(s["step_count"] for s in scenarios) == summary["behave_steps"]
    counts = json.loads((BASELINE / "assert_counts.json").read_text(encoding="utf-8"))
    assert counts["functions"] and counts["steps"]
    assert all(set(v) >= {"asserts", "expects"} for v in counts["functions"].values())


def test_impl_dependent_ids_exist():
    data = tomllib.loads((REPO_ROOT / "tests/blackbox/impl_dependent.toml").read_text(encoding="utf-8"))
    nodeids = {i for v in json.loads((BASELINE / "pytest_nodeids.json").read_text(encoding="utf-8")).values()
               for i in v}
    scenarios = {f'{s["feature"]}::{s["name"]}'
                 for s in json.loads((BASELINE / "behave_scenarios.json").read_text(encoding="utf-8"))}
    assert data["entry"]
    for e in data["entry"]:
        assert e["kind"] in ("behave", "pytest") and e["reason"], e
        if e["kind"] == "behave":
            assert e["id"] in scenarios, e["id"]
        elif e["id"].startswith("tests/blackbox/"):
            path, func = e["id"].split("::")
            assert f"def {func}(" in (REPO_ROOT / path).read_text(encoding="utf-8"), e["id"]
        else:
            assert e["id"] in nodeids, e["id"]


# ================================================================ AM-10 對照腳本(施工審議 T2 F13)

COMPARE = BASELINE / "compare.py"


def _load_compare():
    import importlib.util

    spec = importlib.util.spec_from_file_location("bb_compare", COMPARE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def baseline_snapshot(tmp_path_factory) -> Path:
    """7fadd84 的檔案快照(`git archive`,不動本 repo 的工作樹與 worktree 清單)。"""
    snap = tmp_path_factory.mktemp("snap-7fadd84")
    arch = subprocess.run(["git", "archive", "--format=tar", "7fadd84"], cwd=REPO_ROOT, capture_output=True)
    if arch.returncode != 0:
        pytest.skip(f"無法取得 7fadd84 的快照(git archive 失敗:{arch.stderr.decode(errors='replace')[:200]})")
    subprocess.run(["tar", "-x", "-C", str(snap)], input=arch.stdout, check=True)
    return snap


def _compare(*args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run([sys.executable, str(COMPARE), *args], env=env, capture_output=True, text=True,
                          timeout=600)


def test_compare_passes_on_baseline_commit(baseline_snapshot, tmp_path):
    js = tmp_path / "behave.json"
    # 7fadd84 的 environment.py 自己把快照的 src 與根目錄放到 sys.path 最前面(轉換前的行程內 behave)
    env = {**{k: v for k, v in os.environ.items() if k != "PYTHONPATH"}, "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
    b = subprocess.run([sys.executable, "-m", "behave", "-f", "json", "-o", str(js), "tests/features"],
                       cwd=baseline_snapshot, env=env, capture_output=True, text=True, timeout=600)
    assert b.returncode == 0, b.stdout[-2000:]
    r = _compare("--repo", str(baseline_snapshot), "--no-diff", "--behave-json", str(js))
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
    assert "結果:通過" in r.stdout


def test_compare_fails_when_an_assert_is_removed(baseline_snapshot, tmp_path):
    import shutil

    snap = tmp_path / "reduced"
    shutil.copytree(baseline_snapshot, snap, symlinks=True)
    target = snap / "tests/acceptance/test_nfr3_bind.py"
    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    node = next(n for n in ast.walk(ast.parse("".join(lines)))
                if isinstance(n, ast.Assert) and n.lineno == n.end_lineno)
    indent = lines[node.lineno - 1][: len(lines[node.lineno - 1]) - len(lines[node.lineno - 1].lstrip())]
    lines[node.lineno - 1] = indent + "pass\n"
    target.write_text("".join(lines), encoding="utf-8")
    r = _compare("--repo", str(snap), "--roots", "tests/acceptance", "--no-diff")
    assert r.returncode == 1, r.stdout[-4000:]
    assert "[asserts] tests/acceptance/test_nfr3_bind.py::" in r.stdout and "< 基準" in r.stdout


def test_compare_diff_and_behave_result_checks(tmp_path):
    mod = _load_compare()
    diff = "\n".join([
        "+++ b/tests/ui/test_x.py", "+    pytest.skip('x')", "+@pytest.mark.xfail", "+    except AssertionError:",
        "+    assert '0 skipped' in out", "-    pytest.skip('removed line')", " context skip",
        "+@pytest.mark.skipif(True, reason='x')",
    ])
    assert mod.banned_added_lines(diff) == ["+    pytest.skip('x')", "+@pytest.mark.xfail", "+    except AssertionError:",
                                            "+@pytest.mark.skipif(True, reason='x')"]
    js = tmp_path / "b.json"
    js.write_text(json.dumps([{"location": "f.feature:1", "elements": [
        {"type": "scenario", "status": "passed", "steps": [{"result": {"status": "passed"}}]},
        {"type": "scenario", "status": "skipped", "steps": [{"result": {"status": "skipped"}}, {}]},
    ]}]), encoding="utf-8")
    errors: list[str] = []
    mod.check_behave_json(js, errors, [])
    assert any("skipped" in e for e in errors) and any("untested" in e for e in errors)


def test_control_client_asserts_exact_key_sets():
    """`Control` 對回應的鍵集合做完全比對(F12):多一個或少一個鍵都失敗。"""
    import http.server
    import threading

    bodies = {"/__test__/clock": {"controllable": True, "now": "2026-11-01T12:00:00Z", "extra": 1},
              "/__test__/rng": {"seeded": True},
              "/__test__/cli-slots": {"slots": [{"deployment": "x", "in_flight": 0}]},
              "/__test__/retention": {"runs": 1, "next_due": None}}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            data = json.dumps(bodies[self.path]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        from tests.blackbox.control import Control

        with Control(f"http://127.0.0.1:{srv.server_address[1]}") as c:
            assert c.retention() == {"runs": 1, "next_due": None}
            for call in (c.clock, c.rng, c.cli_slots):
                with pytest.raises(AssertionError, match="鍵"):
                    call()
    finally:
        srv.shutdown()

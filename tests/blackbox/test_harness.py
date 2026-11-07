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

import asyncio
import hashlib
import json
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import tomllib
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.blackbox import guard, plugin
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


def _recording_cmd(tmp_path: Path) -> tuple[str, Path]:
    """`SPILLWAY_CMD` 用的包裝程式:把子命令參數記到檔案,再 exec 預設的 Python 實作。"""
    log = tmp_path / "cmd-log.jsonl"
    script = tmp_path / "rec.py"
    script.write_text(
        "import json, os, sys\n"
        f"with open({str(log)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "os.execv(sys.executable, [sys.executable, '-m', 'spillway', *sys.argv[1:]])\n", encoding="utf-8")
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}", log


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
    script = tmp_path / "stubborn.py"
    script.write_text(
        "import signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "p = subprocess.Popen([sys.executable, '-m', 'spillway', *sys.argv[1:]],\n"
        "                     preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_DFL))\n"
        "p.wait()\n"
        "time.sleep(600)\n", encoding="utf-8")
    monkeypatch.setenv("SPILLWAY_CMD", f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}")
    s = Serve(tmp_path / "home").start()
    with pytest.raises(AssertionError, match="未在 3.0 秒內結束"):
        s.stop(timeout=3.0)
    assert s.returncode == -signal.SIGKILL


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


@pytest.mark.parametrize("imports_spillway", [False, True])
def test_blackbox_guard_fixture_fails_session_that_imports_spillway(tmp_path, imports_spillway):
    """以子行程 pytest 執行一個啟用 `blackbox_guard` 的測試檔(`SPILLWAY_BLACKBOX_GUARD=1` 強制生效)。"""
    test = tmp_path / "test_guarded.py"
    test.write_text(
        "import pytest\n\n"
        "@pytest.fixture(scope='session', autouse=True)\n"
        "def _g(blackbox_guard):\n    yield\n\n"
        "def test_x():\n"
        + ("    __import__('spillway')\n" if imports_spillway else "    pass\n"), encoding="utf-8")
    env = {**os.environ, "SPILLWAY_BLACKBOX_GUARD": "1",
           "PYTHONPATH": os.pathsep.join([str(REPO_ROOT), os.environ.get("PYTHONPATH", "")])}
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "tests.blackbox.plugin", "-p", "no:cacheprovider",
                        str(test)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    if imports_spillway:
        assert r.returncode != 0 and "行程內載入了 spillway" in r.stdout, r.stdout[-2000:]
    else:
        assert r.returncode == 0, r.stdout[-2000:]


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

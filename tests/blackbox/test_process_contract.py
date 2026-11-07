"""行程契約的黑箱測試(CHG-20261107-03 T2;AM-7;spec 14;契約清單「行程契約」)。

經 harness 啟動 `SPILLWAY_CMD`(預設 Python 版):結束碼 1/2/3/130 與 SIGTERM(14.1)、`init` 的結束碼與
stdout/stderr 分工、`TOKEN_NOTICE` 單行的位元組格式與「每個資料庫只印一次、在開始監聽前寫出」(14.2)、
stderr 承諾的行(14.3)、就緒與只綁 127.0.0.1(14.4)、正常關閉等待進行中的請求。不 import spillway。
"""
from __future__ import annotations

import re
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from tests.blackbox.db import db_path, read_db
from tests.blackbox.fakemods import write_module
from tests.blackbox.harness import (
    EXIT_PREFLIGHT, EXIT_SIGINT, EXIT_SIGTERM, EXIT_STARTUP, EXIT_USAGE, KNOB_ENVS, KNOBS_IGNORED_PREFIX,
    TEST_CONTROL_WARNING, TOKEN_NOTICE, Serve, child_env, parse_tokens, run_cli, spillway_cmd,
)
from tests.blackbox.mask import TEST_KEY_ENV
from tests.blackbox.ports import free_port, lan_ip

REPO_ROOT = Path(__file__).resolve().parents[2]
START = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
TOKEN_CHARS = re.compile(r"[A-Za-z0-9_-]{43}")


def _serve_once(home: Path, *args: str, extra: dict[str, str] | None = None,
                timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*spillway_cmd(), "serve", *args], env=child_env(home, extra=extra),
                          cwd=str(home.parent), capture_output=True, text=True, timeout=timeout)


def _spec_token_prefix() -> bytes:
    spec = (REPO_ROOT / "docs" / "spec.md").read_text(encoding="utf-8")
    sec = spec.split("### 14.2", 1)[1].split("### 14.3", 1)[0]
    hexdump = re.search(r"```\n(.*?)```", sec, re.S)
    assert hexdump is not None
    return bytes.fromhex(hexdump.group(1))


# ================================================================ 14.2 TOKEN_NOTICE


def test_token_notice_bytes_match_spec():
    assert TOKEN_NOTICE.encode("utf-8") == _spec_token_prefix()


def test_token_line_once_per_database(tmp_path):
    home = tmp_path / "home"
    with Serve(home) as s:
        lines = [ln for ln in s.stderr_bytes().split(b"\n") if ln.startswith(_spec_token_prefix())]
        assert len(lines) == 1
        token = lines[0][len(_spec_token_prefix()):].decode("ascii")
        assert TOKEN_CHARS.fullmatch(token) and token == s.token
        assert (lines[0] + b"\n") in s.stderr_bytes()   # LF 結尾的整行
    with Serve(home) as s2:   # 同一資料庫:之後的啟動不印
        assert parse_tokens(s2.stderr_bytes()) == [] and s2.token is None
        assert _spec_token_prefix() not in s2.stderr_bytes()


def test_token_line_written_before_listening(tmp_path):
    s = Serve(tmp_path / "home").start(wait_ready=False)
    try:
        deadline = time.monotonic() + 60
        while True:
            assert s.returncode is None and time.monotonic() < deadline
            try:
                socket.create_connection(("127.0.0.1", s.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.01)
        # 已能連線:該行已寫出並 flush。讀 stderr 的執行緒只會稍有延遲(不超過 0.25 秒)
        grace = time.monotonic() + 0.25
        while not parse_tokens(s.stderr_bytes()) and time.monotonic() < grace:
            time.sleep(0.005)
        assert len(parse_tokens(s.stderr_bytes())) == 1
        s.wait_ready()
    finally:
        s.kill()


# ================================================================ 14.3 stderr 承諾的行


def test_control_warning_line(serve_factory):
    s = serve_factory()
    assert TEST_CONTROL_WARNING in s.stderr_text().splitlines()
    assert not [ln for ln in s.stderr_text().splitlines() if ln.startswith(KNOBS_IGNORED_PREFIX)]


def test_ignored_knobs_line_lists_names_only(serve_factory):
    values = {"SPILLWAY_TEST_START_TIME": "2026-11-01T12:00:00Z", "SPILLWAY_TEST_RNG_SEED": "424242",
              "SPILLWAY_TEST_NO_TOKEN": "1", "SPILLWAY_TEST_LOG_LEVEL": "DEBUG",
              "SPILLWAY_TEST_RETENTION_POLL_SECONDS": ""}   # 空字串等同未設定
    s = serve_factory(control=False, extra_env=values)
    lines = s.stderr_text().splitlines()
    ignored = [ln for ln in lines if ln.startswith(KNOBS_IGNORED_PREFIX)]
    expect = [n for n in KNOB_ENVS if values.get(n)]
    assert ignored == [KNOBS_IGNORED_PREFIX + ", ".join(expect)]
    assert TEST_CONTROL_WARNING not in lines
    text = s.stderr_text()
    assert "2026-11-01T12:00:00Z" not in text and "424242" not in text   # 不印值
    assert not [ln for ln in lines if ln.startswith("SPILLWAY-LOG")]       # 旋鈕未生效
    assert s.request("GET", "/api/settings").json()["token_issued"] is True


@pytest.mark.usefixtures("fake_key_env")
@pytest.mark.parametrize("level", ["DEBUG", "INFO"])
def test_log_level_lines_format(serve_factory, fake_api, level):
    s = serve_factory(log_level=level)
    body = {"id": "api-a", "model_name": "api-a", "kind": "api", "provider": "openai", "model": "gpt-4o-mini",
            "params": {"api_base": fake_api.base_url + "/v1"}, "credential_ref": f"env:{TEST_KEY_ENV}",
            "capabilities": {"ctx": 128000, "tools": True, "vision": False, "efforts": []}, "tags": ["chat"]}
    assert s.request("POST", "/api/deployments", json=body).status_code == 201
    assert s.request("POST", "/api/routes", json={"name": "r", "target": {"tags": {"any": ["chat"]}}}).status_code == 201
    r = s.request("POST", "/v1/chat/completions", json={"model": "r", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    s.drain_stderr()
    lines = s.stderr_text().splitlines()
    log = [ln for ln in lines if ln.startswith("SPILLWAY-LOG")]
    first = re.compile(r"SPILLWAY-LOG (DEBUG|INFO|WARNING|ERROR|CRITICAL) \S+ .*")
    for ln in log:
        assert first.fullmatch(ln) or ln.startswith("SPILLWAY-LOG-CONT "), ln
    levels = {m.group(1) for ln in log if (m := first.fullmatch(ln))}
    if level == "DEBUG":
        assert "DEBUG" in levels
    else:
        assert "DEBUG" not in levels   # 低於設定等級的紀錄不寫出


# ================================================================ 14.1 結束碼


@pytest.mark.parametrize("args", [["--host", "0.0.0.0"], ["--port", "0"], ["--port", "abc"], ["--bogus"]])
def test_exit_2_usage_errors_without_database(tmp_path, args):
    home = tmp_path / "home"
    r = _serve_once(home, *args)
    assert r.returncode == EXIT_USAGE, r.stderr
    assert not db_path(home).exists()


def test_exit_2_invalid_knob_value_without_database(tmp_path):
    home = tmp_path / "home"
    r = _serve_once(home, "--port", str(free_port()),
                    extra={"SPILLWAY_TEST_CONTROL": "1", "SPILLWAY_TEST_RNG_SEED": "12x34"})
    assert r.returncode == EXIT_USAGE
    assert "SPILLWAY_TEST_RNG_SEED" in r.stderr and "12x34" not in r.stderr
    assert not db_path(home).exists()


def test_exit_1_port_in_use_without_database(tmp_path):
    home = tmp_path / "home"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        r = _serve_once(home, "--port", str(busy.getsockname()[1]))
    assert r.returncode == EXIT_PREFLIGHT, r.stderr
    assert not db_path(home).exists()


def test_exit_1_data_dir_locked(serve_factory):
    s = serve_factory()
    r = _serve_once(s.home, "--port", str(free_port()))
    assert r.returncode == EXIT_PREFLIGHT, r.stderr
    assert s.request("GET", "/api/settings/context").status_code == 200   # 原本的 serve 不受影響


def test_exit_3_on_adapter_conflict(tmp_path):
    """7.6.6:同一 provider 被兩個模組宣告 → 拒絕啟動(結束碼 3)。以 Python 測試模組觸發(實作相依)。"""
    mods = tmp_path / "mods"
    write_module(mods, "dup_openai", providers=("openai",))
    r = _serve_once(tmp_path / "home", "--port", str(free_port()),
                    extra={"SPILLWAY_TEST_CONTROL": "1", "SPILLWAY_TEST_ADAPTER_PATHS": str(mods)})
    assert r.returncode == EXIT_STARTUP, r.stderr[-2000:]


def test_sigterm_ends_by_signal(serve_factory):
    s = serve_factory()
    assert s.stop(signal.SIGTERM) == EXIT_SIGTERM == -15


def test_sigint_exit_130_without_traceback(serve_factory):
    s = serve_factory()
    assert s.stop(signal.SIGINT) == EXIT_SIGINT
    assert "Traceback" not in s.stderr_text()


@pytest.mark.usefixtures("fake_key_env")
def test_graceful_shutdown_waits_for_in_flight_request(serve_factory, fake_api):
    """正常關閉:停止接受新請求,等待進行中的請求(至多停機上限)。"""
    s = serve_factory(graceful_timeout=10.0)
    body = {"id": "api-slow", "model_name": "api-slow", "kind": "api", "provider": "openai", "model": "slow-model",
            "params": {"api_base": fake_api.base_url + "/v1", "timeout": 30}, "credential_ref": f"env:{TEST_KEY_ENV}",
            "capabilities": {"ctx": 128000, "tools": True, "vision": False, "efforts": []}, "tags": ["slow"]}
    assert s.request("POST", "/api/deployments", json=body).status_code == 201
    assert s.request("POST", "/api/routes", json={"name": "slow", "target": {"tags": {"any": ["slow"]}}}).status_code == 201
    fake_api.set_mode("slow-model", "delay", seconds=1.5)
    box: dict[str, httpx.Response] = {}

    def call() -> None:
        box["r"] = s.request("POST", "/v1/chat/completions",
                             json={"model": "slow", "messages": [{"role": "user", "content": "hi"}]})

    t = threading.Thread(target=call)
    t.start()
    deadline = time.monotonic() + 10
    while not fake_api.requests and time.monotonic() < deadline:
        time.sleep(0.02)
    assert fake_api.requests, "請求未送到假上游"
    assert s.stop(signal.SIGTERM) == EXIT_SIGTERM
    t.join(15)
    assert box["r"].status_code == 200, box["r"].text


# ================================================================ init


def test_init_exit_codes_and_streams(tmp_path):
    home = tmp_path / "home"
    r = run_cli("init", home=home)
    assert r.returncode == 0, r.stderr
    out = r.stdout.splitlines()
    assert len(out) == 1 and TOKEN_CHARS.fullmatch(out[0])
    token = out[0]
    assert token not in r.stderr
    r2 = run_cli("init", home=home)
    assert r2.returncode == 0 and r2.stdout == ""          # 已存在,未變更
    r3 = run_cli("init", "--rotate", home=home)
    assert r3.returncode == 0 and TOKEN_CHARS.fullmatch(r3.stdout.strip()) and r3.stdout.strip() != token
    assert run_cli("init", "--bogus", home=home).returncode == EXIT_USAGE
    with Serve(home) as s:
        assert s.token is None                              # init 已簽發:serve 不印
        assert run_cli("init", home=home).returncode == EXIT_PREFLIGHT   # serve 持有資料目錄鎖
    with read_db(home) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 2


# ================================================================ 14.4 就緒與綁定


def test_ready_and_bound_to_loopback_only(serve_factory):
    s = serve_factory()
    assert httpx.get(s.url("/api/settings/context"), trust_env=False).status_code == 200
    ip = lan_ip()
    if ip is not None:   # 沒有非 loopback 位址的機器只驗證 127.0.0.1 可連
        with pytest.raises(OSError):
            socket.create_connection((ip, s.port), timeout=1).close()

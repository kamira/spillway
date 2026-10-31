"""`spillway serve`(CHG-20261030-02 T13;NFR-3;決策 3、4、21;design D16)。

L1:`--host` 只接受字面 127.0.0.1;其他位址、不合法埠號、啟動失敗皆以非 0 退出。
L4:以子行程啟動 `python -m spillway serve`,以 lsof 確認只在 127.0.0.1 監聽,經 TCP 打一次成功。
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from spillway import cli
from spillway.store import repo
from spillway.store.db import connect
from tests.contract.fakemods import write_module
from tests.integration.conftest import local_member

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
BAD_HOSTS = ["0.0.0.0", "localhost", "::1", "::", "192.168.1.10"]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    env["SPILLWAY_HOME"] = str(home)
    env["PYTHONPATH"] = str(SRC)
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    return env


def _serve(home: Path, *args: str, **kw) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "spillway", "serve", *args], env=_env(home),
                            cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, **kw)


# ---- L1:在行程內呼叫 main() ------------------------------------------------------------

@pytest.mark.parametrize("host", BAD_HOSTS)
def test_main_rejects_non_loopback_host(host, monkeypatch, capsys):
    started = []
    monkeypatch.setattr(cli, "_run_server", lambda *a, **k: started.append(a) or 0)
    code = cli.main(["serve", "--host", host, "--port", "8787"])
    assert code != 0
    assert started == []                      # 沒有啟動伺服器
    assert "127.0.0.1" in capsys.readouterr().err


def test_main_accepts_loopback_and_defaults(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_run_server", lambda host, port: seen.append((host, port)) or 0)
    assert cli.main(["serve"]) == 0
    assert cli.main(["serve", "--host", "127.0.0.1", "--port", "9999"]) == 0
    assert seen == [("127.0.0.1", 8787), ("127.0.0.1", 9999)]


@pytest.mark.parametrize("port", ["0", "70000", "abc"])
def test_main_rejects_bad_port(port, monkeypatch):
    monkeypatch.setattr(cli, "_run_server", lambda *a, **k: 0)
    assert cli.main(["serve", "--port", port]) != 0


def test_main_without_command_is_nonzero(capsys):
    assert cli.main([]) != 0


def test_startup_failure_exits_nonzero(tmp_path, monkeypatch):
    """7.6.6 衝突(兩個模組宣告同一 provider)→ lifespan 失敗 → serve 非 0 退出。"""
    from spillway import app as app_mod

    mods = tmp_path / "mods"
    write_module(mods, "dup_openai", providers=("openai",))
    real = app_mod.create_app
    monkeypatch.setattr(app_mod, "create_app", lambda **kw: real(extra_adapter_paths=[mods], **kw))
    assert cli.main(["serve", "--port", str(_free_port())]) != 0


# ---- L4:子行程 ------------------------------------------------------------------------

@pytest.mark.parametrize("host", BAD_HOSTS)
def test_subprocess_rejects_non_loopback_host(host, tmp_path):
    p = _serve(tmp_path, "--host", host, "--port", str(_free_port()))
    try:
        out, err = p.communicate(timeout=60)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
    assert p.returncode != 0
    assert "127.0.0.1" in err


def _listening(pid: int) -> list[str]:
    r = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", str(pid)],
                       capture_output=True, text=True)
    lines = r.stdout.splitlines()[1:]
    # NAME 欄在最後一欄之前:"... TCP 127.0.0.1:8787 (LISTEN)"
    return [ln.split()[-2] for ln in lines if "(LISTEN)" in ln]


@pytest.mark.skipif(shutil.which("lsof") is None, reason="需要 lsof(macOS 驗收平台)")
def test_serve_binds_only_loopback_and_serves(tmp_path, fake_api):
    home = tmp_path / "home"
    home.mkdir()
    conn = connect(home / "spillway.db")
    try:
        repo.save_deployment(conn, local_member("loc-1", fake_api.base_url))
    finally:
        conn.close()

    port = _free_port()
    p = _serve(home, "--port", str(port))
    try:
        addrs: list[str] = []
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if p.poll() is not None:
                out, err = p.communicate()
                pytest.fail(f"serve 提前結束 rc={p.returncode}\n{err}")
            addrs = _listening(p.pid)
            if addrs:
                break
            time.sleep(0.2)
        assert addrs == [f"127.0.0.1:{port}"], addrs

        r = httpx.post(f"http://127.0.0.1:{port}/v1/chat/completions", timeout=30,
                       json={"model": "local/loc-1", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        assert r.json()["choices"][0]["message"]["content"] == "fake upstream ok"
        assert r.headers["X-Spillway-Slot"] == "direct"
        assert r.headers["X-Spillway-Served-By"] == "loc-1"
    finally:
        p.terminate()
        try:
            p.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
    assert p.returncode is not None

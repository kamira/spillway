"""存取 token、`spillway init` 與 serve 啟動前置檢查(CHG-20261101-03 T7;決策 5、14、15、17)。

L1(行程內):`init` 建立 `work/claude`、`work/codex`、產生 token(只存 SHA-256)、第二次印「已存在,未變更」
並以 0 退出、`--rotate` 改變雜湊、在 v1 庫上 migrate 並備份、鎖被占時不 migrate;
`create_app(issue_token=True)` 的 lifespan 首次產生並在 stderr 印一行、之後不再印;
`_preflight` 先試綁埠再取鎖,任一失敗則 `_run_server` 不建 app、不 migrate。
L2(子行程,環境一律經 `safe_subprocess_env`):`spillway init`、`spillway serve` 首次啟動、
rotate 後 serve 的輸出(含 uvicorn access log)與資料目錄中沒有 token 明文;埠被占用或鎖被占用時
serve 非 0 退出且 v1 測試庫仍為 v1。
"""
from __future__ import annotations

import fcntl
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from spillway import app as app_module
from spillway import cli, settings
from spillway.store import db as store_db
from spillway.store.db import LOCK_FILENAME, acquire_data_dir_lock, connect, release_data_dir_lock
from tests.fakes.cli_upstream import safe_subprocess_env

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
REAL_KEY_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
EXISTS_MSG = "已存在,未變更"


# ---- 輔助 ---------------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _occupied_port() -> Iterator[int]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        yield s.getsockname()[1]
    finally:
        s.close()


@contextmanager
def _foreign_lock(data_dir: Path) -> Iterator[None]:
    """另開一個檔案描述取得 `spillway.lock`(等同另一個行程持有;flock 以開啟的檔案為單位)。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(data_dir / LOCK_FILENAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _make_v1(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path), isolation_level=None)
    try:
        c.execute("BEGIN")
        for stmt in store_db._split_statements(store_db.MIGRATIONS[0][1]):
            c.execute(stmt)
        c.execute("PRAGMA user_version = 1")
        c.execute("INSERT INTO deployments(id, model_name, kind, provider, model) "
                  "VALUES ('d1', 'm', 'api', 'openai', 'gpt')")
        c.execute("INSERT INTO budgets(deployment_id, type, limit_amount, window, reset_anchor, "
                  "exhausted_until) VALUES ('d1', 'usd', 5.0, 'daily', NULL, '2026-10-30T12:00:00Z')")
        c.execute("COMMIT")
    finally:
        c.close()


def _user_version(path: Path) -> int:
    c = sqlite3.connect(str(path))
    try:
        return c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        c.close()


def _token_hash(path: Path) -> str | None:
    c = connect(path)
    try:
        return settings.get_access_token_hash(c)
    finally:
        c.close()


def _backups(home: Path) -> list[Path]:
    return sorted(home.glob("spillway.db.v1.bak*"))


def _files_containing(root: Path, needle: str) -> list[Path]:
    data = needle.encode()
    return [p for p in root.rglob("*") if p.is_file() and data in p.read_bytes()]


def _notice_tokens(text: str) -> list[str]:
    return [ln[len(app_module.TOKEN_NOTICE):].strip() for ln in text.splitlines()
            if ln.startswith(app_module.TOKEN_NOTICE)]


@pytest.fixture
def home() -> Path:
    return settings.data_dir()   # 根 conftest 已指向暫存目錄


# ---- L1:init(行程內) ----------------------------------------------------------------------

def test_init_creates_dirs_and_token(home, capsys):
    assert cli.main(["init"]) == 0
    out, err = capsys.readouterr()
    token = out.strip()
    assert len(token) == 43
    assert (home / "work" / "claude").is_dir() and (home / "work" / "codex").is_dir()
    assert _token_hash(home / "spillway.db") == settings.hash_access_token(token)
    assert _user_version(home / "spillway.db") == store_db.SCHEMA_VERSION
    assert token not in err


def test_init_twice_is_idempotent(home, capsys):
    assert cli.main(["init"]) == 0
    token = capsys.readouterr().out.strip()
    before = _token_hash(home / "spillway.db")
    assert cli.main(["init"]) == 0
    out, err = capsys.readouterr()
    assert out == ""                 # `TOKEN=$(spillway init)` 不會拿到提示文字
    assert EXISTS_MSG in err
    assert token not in err
    assert _token_hash(home / "spillway.db") == before


def test_init_rotate_lock_held_keeps_hash(home, capsys):
    assert cli.main(["init"]) == 0
    before = _token_hash(home / "spillway.db")
    capsys.readouterr()
    with _foreign_lock(home):
        assert cli.main(["init", "--rotate"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "serve" in err
    assert _token_hash(home / "spillway.db") == before


def _make_newer(path: Path) -> None:
    connect(path).close()
    c = sqlite3.connect(str(path), isolation_level=None)
    try:
        c.execute(f"PRAGMA user_version = {store_db.SCHEMA_VERSION + 1}")
    finally:
        c.close()


def test_init_newer_schema_exits_1(home, capsys):
    _make_newer(home / "spillway.db")
    assert cli.main(["init"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "SchemaVersionError" in err
    assert _user_version(home / "spillway.db") == store_db.SCHEMA_VERSION + 1


def test_init_rotate_changes_hash(home, capsys):
    assert cli.main(["init"]) == 0
    first = capsys.readouterr().out.strip()
    assert cli.main(["init", "--rotate"]) == 0
    second = capsys.readouterr().out.strip()
    assert second != first and len(second) == 43
    assert _token_hash(home / "spillway.db") == settings.hash_access_token(second)


def test_init_migrates_v1_with_backup(home, capsys):
    _make_v1(home / "spillway.db")
    assert cli.main(["init"]) == 0
    assert _user_version(home / "spillway.db") == 2
    assert [p.name for p in _backups(home)] == ["spillway.db.v1.bak"]
    assert _user_version(home / "spillway.db.v1.bak") == 1


def test_init_lock_held_does_not_migrate(home, capsys):
    _make_v1(home / "spillway.db")
    with _foreign_lock(home):
        assert cli.main(["init"]) != 0
    out, err = capsys.readouterr()
    assert "serve" in err
    assert out == ""
    assert _user_version(home / "spillway.db") == 1
    assert _backups(home) == []


def test_init_releases_lock(home, capsys):
    assert cli.main(["init"]) == 0
    with _foreign_lock(home):   # init 結束後鎖已釋放
        pass


# ---- L1:serve 啟動時的 token(行程內 lifespan) ------------------------------------------

@pytest.mark.anyio
async def test_lifespan_issues_token_once(home, clock, capfd, caplog):
    caplog.set_level(logging.DEBUG)
    path = home / "spillway.db"
    capfd.readouterr()
    app = app_module.create_app(clock=clock, db_path=path, issue_token=True)
    async with app.router.lifespan_context(app):
        pass
    out, err = capfd.readouterr()
    tokens = _notice_tokens(err)
    assert len(tokens) == 1
    assert err.count(tokens[0]) == 1          # 只印一次
    assert tokens[0] not in out
    assert _token_hash(path) == settings.hash_access_token(tokens[0])
    assert tokens[0] not in caplog.text
    app2 = app_module.create_app(clock=clock, db_path=path, issue_token=True)
    async with app2.router.lifespan_context(app2):
        pass
    assert _notice_tokens(capfd.readouterr().err) == []
    assert _token_hash(path) == settings.hash_access_token(tokens[0])


@pytest.mark.anyio
async def test_lifespan_default_does_not_issue_token(home, clock, capfd):
    path = home / "spillway.db"
    app = app_module.create_app(clock=clock, db_path=path)
    async with app.router.lifespan_context(app):
        pass
    assert _notice_tokens(capfd.readouterr().err) == []
    assert _token_hash(path) is None


# ---- L1:_preflight ------------------------------------------------------------------------

def test_preflight_ok_takes_lock(home):
    try:
        assert cli._preflight("127.0.0.1", _free_port(), home) == 0
        with pytest.raises(BlockingIOError):
            with _foreign_lock(home):
                pass
    finally:
        release_data_dir_lock(home)


def test_preflight_port_occupied_does_not_lock(home, capsys):
    with _occupied_port() as port:
        assert cli._preflight("127.0.0.1", port, home) != 0
    assert str(port) in capsys.readouterr().err
    with _foreign_lock(home):   # 沒有取鎖
        pass


def test_preflight_lock_held(home, capsys):
    with _foreign_lock(home):
        assert cli._preflight("127.0.0.1", _free_port(), home) != 0
    assert "serve" in capsys.readouterr().err


def test_run_server_preflight_failure_skips_app(home, monkeypatch):
    _make_v1(home / "spillway.db")
    built = []
    monkeypatch.setattr(app_module, "create_app", lambda **kw: built.append(kw))
    with _occupied_port() as port:
        assert cli._run_server("127.0.0.1", port) != 0
    with _foreign_lock(home):
        assert cli._run_server("127.0.0.1", _free_port()) != 0
    assert built == []
    assert _user_version(home / "spillway.db") == 1
    assert _backups(home) == []


def test_refusal_message_says_m3(capsys):
    assert cli.main(["serve", "--host", "0.0.0.0"]) != 0
    assert "M3 仍只允許 127.0.0.1" in capsys.readouterr().err


# ---- L2:子行程 -----------------------------------------------------------------------------

def _env(home: Path) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if k not in REAL_KEY_VARS}
    base["SPILLWAY_HOME"] = str(home)
    base["PYTHONPATH"] = str(SRC)
    base["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    return safe_subprocess_env(base)


def _run(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "spillway", *args], env=_env(home),
                          cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)


def _serve(home: Path, port: int) -> subprocess.Popen[str]:
    return subprocess.Popen([sys.executable, "-m", "spillway", "serve", "--port", str(port)],
                            env=_env(home), cwd=str(REPO_ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def _stop(p: subprocess.Popen[str]) -> tuple[str, str]:
    if p.poll() is None:
        p.terminate()
    try:
        return p.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        p.kill()
        return p.communicate()


def _wait_ready(p: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if p.poll() is not None:
            out, err = p.communicate()
            pytest.fail(f"serve 提前結束 rc={p.returncode}\n{err}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/settings/context", timeout=2).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    pytest.fail("serve 未在 60 秒內就緒")


def test_subprocess_init_prints_token_once(home):
    r = _run(home, "init")
    assert r.returncode == 0, r.stderr
    token = r.stdout.strip()
    assert len(token) == 43
    assert r.stdout.count(token) == 1 and token not in r.stderr
    assert _token_hash(home / "spillway.db") == settings.hash_access_token(token)
    r2 = _run(home, "init")
    assert r2.returncode == 0, r2.stderr
    assert r2.stdout == ""
    assert EXISTS_MSG in r2.stderr
    assert token not in r2.stderr
    assert _token_hash(home / "spillway.db") == settings.hash_access_token(token)
    assert (home / "work" / "claude").is_dir() and (home / "work" / "codex").is_dir()
    assert _files_containing(home, token) == []


def test_subprocess_serve_issues_token_and_rotate_leaves_no_plaintext(home):
    port = _free_port()
    p = _serve(home, port)
    try:
        _wait_ready(p, port)
        stored = _token_hash(home / "spillway.db")
        assert stored is not None
        r = httpx.post(f"http://127.0.0.1:{port}/api/settings/token/rotate", timeout=30)
        assert r.status_code == 200, r.text
        rotated = r.json()["token"]
        assert _token_hash(home / "spillway.db") == settings.hash_access_token(rotated)
        assert httpx.get(f"http://127.0.0.1:{port}/api/dashboard", timeout=30).status_code == 200
    finally:
        out, err = _stop(p)
    first = _notice_tokens(err)
    assert len(first) == 1, err
    assert settings.hash_access_token(first[0]) == stored
    assert first[0] not in out and err.count(first[0]) == 1
    assert "POST /api/settings/token/rotate" in out + err          # access log 有記錄、但不含 body
    assert rotated not in out + err
    assert _files_containing(home, rotated) == []
    assert _files_containing(home, first[0]) == []
    artifacts = REPO_ROOT / "artifacts"
    if artifacts.is_dir():
        assert _files_containing(artifacts, rotated) == []

    # 第二次啟動:已有雜湊,不再印
    port2 = _free_port()
    p2 = _serve(home, port2)
    try:
        _wait_ready(p2, port2)
    finally:
        _, err2 = _stop(p2)
    assert _notice_tokens(err2) == []
    assert _token_hash(home / "spillway.db") == settings.hash_access_token(rotated)


def test_subprocess_serve_port_occupied_does_not_migrate(home):
    _make_v1(home / "spillway.db")
    with _occupied_port() as port:
        r = _run(home, "serve", "--port", str(port))
    assert r.returncode != 0
    assert str(port) in r.stderr
    assert _user_version(home / "spillway.db") == 1
    assert _backups(home) == []


def test_subprocess_serve_lock_held_does_not_migrate(home):
    _make_v1(home / "spillway.db")
    with _foreign_lock(home):
        r = _run(home, "serve", "--port", str(_free_port()))
    assert r.returncode != 0
    assert "serve" in r.stderr
    assert _user_version(home / "spillway.db") == 1
    assert _backups(home) == []


def test_subprocess_serve_newer_schema_exits_3(home):
    """資料庫比程式新:lifespan 的 SchemaVersionError → 啟動失敗,uvicorn 以 3 退出。"""
    _make_newer(home / "spillway.db")
    p = _serve(home, _free_port())
    try:
        out, err = p.communicate(timeout=60)
    finally:
        if p.poll() is None:
            p.kill()
            p.communicate()
    assert p.returncode == 3, err
    assert _user_version(home / "spillway.db") == store_db.SCHEMA_VERSION + 1
    assert _notice_tokens(err) == []


def test_subprocess_serve_lock_held_by_in_process_holder(home):
    """測試行程以 `acquire_data_dir_lock` 持有(模擬已在執行的 serve)時,子行程 serve 同樣退出。"""
    _make_v1(home / "spillway.db")
    acquire_data_dir_lock(home)
    try:
        r = _run(home, "serve", "--port", str(_free_port()))
    finally:
        release_data_dir_lock(home)
    assert r.returncode != 0
    assert _user_version(home / "spillway.db") == 1

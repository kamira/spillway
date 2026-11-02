import hashlib
import re
from pathlib import Path

import platformdirs
import pytest

from spillway import settings
from spillway.store.db import connect


def test_data_dir_uses_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SPILLWAY_HOME", str(tmp_path / "x"))
    assert settings.data_dir() == tmp_path / "x"
    assert settings.db_path() == tmp_path / "x" / "spillway.db"


def test_data_dir_falls_back_to_platformdirs(monkeypatch):
    monkeypatch.delenv("SPILLWAY_HOME", raising=False)
    assert settings.data_dir() == Path(platformdirs.user_data_dir("spillway"))


def test_defaults():
    assert settings.DEFAULT_PORT == 8787
    assert settings.DEFAULT_HOST == "127.0.0.1"
    assert settings.is_allowed_host("127.0.0.1")
    for host in ("localhost", "::1", "::", "0.0.0.0", "192.168.1.10"):
        assert not settings.is_allowed_host(host)


# ---- CHG-20261101-03 T7 L1:存取 token(決策 14、17) ----------------------------------------

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    try:
        yield c
    finally:
        c.close()


def _stored(conn) -> list[tuple[str, str]]:
    return [tuple(r) for r in conn.execute("SELECT key, value FROM settings").fetchall()]


def test_m3_still_only_loopback():
    assert settings.ALLOWED_HOSTS == frozenset({"127.0.0.1"})


def test_token_uses_secrets_token_urlsafe_32(monkeypatch):
    seen = []
    real = settings.secrets.token_urlsafe
    monkeypatch.setattr(settings.secrets, "token_urlsafe", lambda n: seen.append(n) or real(n))
    token = settings.generate_access_token()
    assert seen == [32]
    assert TOKEN_RE.match(token)


def test_hash_is_sha256_hex():
    assert settings.hash_access_token("abc") == hashlib.sha256(b"abc").hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", settings.hash_access_token("x"))


def test_ensure_generates_once_and_stores_only_hash(conn):
    assert settings.get_access_token_hash(conn) is None
    token = settings.ensure_access_token(conn)
    assert token is not None and TOKEN_RE.match(token)
    assert _stored(conn) == [("access_token_sha256", hashlib.sha256(token.encode()).hexdigest())]
    assert all(token not in v for _, v in _stored(conn))
    assert settings.ensure_access_token(conn) is None          # 已存在:不變更
    assert settings.get_access_token_hash(conn) == settings.hash_access_token(token)


def test_rotate_changes_hash(conn):
    first = settings.ensure_access_token(conn)
    second = settings.rotate_access_token(conn)
    assert second != first
    assert settings.get_access_token_hash(conn) == settings.hash_access_token(second)
    assert len(_stored(conn)) == 1


def test_serve_ignores_bind_host_and_port_rows(monkeypatch):
    """D16 不變(決策 17):資料目錄的資料庫有 bind_host、port 列時,serve 仍用 127.0.0.1:8787。"""
    from spillway import cli

    c = connect(settings.db_path())
    try:
        c.execute("INSERT INTO settings(key, value) VALUES ('bind_host', '0.0.0.0'), ('port', '1')")
    finally:
        c.close()
    seen = []
    monkeypatch.setattr(cli, "_run_server", lambda host, port: seen.append((host, port)) or 0)
    assert cli.main(["serve"]) == 0
    assert seen == [("127.0.0.1", 8787)]

from pathlib import Path

import platformdirs

from spillway import settings


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

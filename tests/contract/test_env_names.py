"""環境變數名與 depth header 名的單一定義(CHG-20261102-02 T2;決策 15,決策審議修訂 AM-16)。

`spillway.settings` 定義 `TOKEN_ENV`、`DEPTH_ENV`、`DEPTH_HEADER`;`api.mcp.{auth,bridge,catalog}` 與
`api.admin.mcp_config` 引用它們。adapters 依分層規則(只可 import `adapters.base`)保留字面值,
`templates/mcp/*.j2` 為模板字面值——兩者由本檔釘住與 settings 相等。
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import spillway
from spillway import settings

SRC = Path(spillway.__file__).resolve().parent
TEMPLATES = sorted((SRC / "templates" / "mcp").glob("*.j2"))
_SPILLWAY_NAME = re.compile(r"SPILLWAY_[A-Z0-9_]+")


def test_settings_constants():
    assert settings.TOKEN_ENV == "SPILLWAY_TOKEN"
    assert settings.DEPTH_ENV == "SPILLWAY_DEPTH"
    assert settings.DEPTH_HEADER == "X-Spillway-Depth"


@pytest.mark.parametrize("module", ["claude_cli", "codex_cli"])
def test_adapter_env_set_key_is_depth_env(module):
    import importlib

    adapter = importlib.import_module(f"spillway.adapters.{module}.adapter")
    assert set(adapter.ENV_SET) == {settings.DEPTH_ENV}


def test_auth_has_no_depth_header_literal_and_uses_settings():
    from spillway.api.mcp import auth

    text = (SRC / "api" / "mcp" / "auth.py").read_text(encoding="utf-8")
    assert "spillway-depth" not in text.lower()
    raw = settings.DEPTH_HEADER.lower().encode()
    assert auth.check_depth([(raw, b"0")]) is True
    assert auth.check_depth([(raw, b"1")]) is False


def test_bridge_uses_settings_constants():
    from spillway.api.mcp import bridge

    assert bridge.TOKEN_ENV is settings.TOKEN_ENV
    assert bridge.DEPTH_ENV is settings.DEPTH_ENV
    assert bridge.DEPTH_HEADER is settings.DEPTH_HEADER
    headers = bridge.build_headers({settings.TOKEN_ENV: "spw_x", settings.DEPTH_ENV: "1"})
    assert headers == {"Authorization": "Bearer spw_x", settings.DEPTH_HEADER: "1"}


def test_mcp_config_env_vars():
    from spillway.api.admin import mcp_config

    assert mcp_config.ENV_VARS == [settings.TOKEN_ENV]


@pytest.mark.parametrize("path", [*TEMPLATES, SRC / "api" / "mcp" / "catalog.py"],
                         ids=lambda p: p.name)
def test_token_names_in_templates_and_catalog_equal_token_env(path):
    names = set(_SPILLWAY_NAME.findall(path.read_text(encoding="utf-8")))
    assert names <= {settings.TOKEN_ENV}, names


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_every_template_references_token_env(path):
    assert settings.TOKEN_ENV in path.read_text(encoding="utf-8")


def test_catalog_output_uses_token_env_and_depth_header(tmp_path):
    from spillway.api.mcp.catalog import mcp_catalog
    from spillway.store.db import connect

    db = tmp_path / "spillway.db"
    connect(db).close()
    app = SimpleNamespace(state=SimpleNamespace(port=8787, conn_factory=lambda: connect(db)))
    conn = mcp_catalog(app)["connection"]
    assert conn["auth"]["env"] == settings.TOKEN_ENV
    assert conn["auth"]["value"] == f"Bearer ${{{settings.TOKEN_ENV}}}"
    assert conn["stdio"]["env"] == [settings.TOKEN_ENV]
    recursion = next(e for e in mcp_catalog(app)["errors"]["http"] if e["code"] == "recursion_blocked")
    assert recursion["description"].startswith(settings.DEPTH_HEADER)

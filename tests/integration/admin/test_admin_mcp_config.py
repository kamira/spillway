"""`GET /api/mcp/config`(CHG-20261102-01 T5;決策 3、16、17;spec 8.2)。

L1:5 種工具的回應形狀、各 format 可解析、跳脫、錯誤碼、不含 token。
L2:以 `template_dir` 注入模板目錄,只加檔案 + 一行 `TOOL_FORMATS` 即多一種工具。
"""
from __future__ import annotations

import json
import shlex
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from spillway.api.admin import mcp_config
from spillway.api.admin.mcp_config import TOOL_FORMATS, VariantSpec, build_context, render_config

pytestmark = pytest.mark.anyio

TOOLS = ["claude-code", "codex", "cursor", "gemini", "stdio"]
EVIL = 'a"b;$(x)'
TOP = {"group", "tool", "variants", "env", "notes", "format", "path", "content"}
VARIANT = {"label", "scope", "format", "path", "filename", "content"}


async def _get(client, tool, group="all"):
    return await client.get("/api/mcp/config", params={"group": group, "tool": tool})


def _parse(v):
    fmt = v["format"]
    if fmt == "json":
        return json.loads(v["content"])
    if fmt == "toml":
        return tomllib.loads(v["content"])
    return shlex.split(v["content"])


async def _mk_group(client, name):
    r = await client.post("/api/groups", json={"name": name, "selector": {"any": ["x"]}})
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("tool", TOOLS)
async def test_shape_and_parse(client, tool):
    r = await _get(client, tool)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == TOP and body["tool"] == tool and body["group"] == "all"
    assert body["env"] == ["SPILLWAY_TOKEN"]
    assert body["variants"] and all(set(v) == VARIANT for v in body["variants"])
    first = body["variants"][0]
    assert (body["format"], body["path"], body["content"]) == (first["format"], first["path"], first["content"])
    for v in body["variants"]:
        assert v["scope"] in ("user", "project") and v["format"] in ("command", "json", "toml")
        assert _parse(v)
        assert "SPILLWAY_DEPTH" not in v["content"]
    for n in body["notes"]:
        assert set(n) == {"severity", "text"} and n["severity"] in ("info", "warn")


async def test_url_name_and_port(client, make_app, make_client):
    r = (await _get(client, "cursor")).json()
    cfg = json.loads(r["content"])
    assert cfg["mcpServers"] == {"spillway-all": {
        "url": "http://127.0.0.1:8787/mcp/all",
        "headers": {"Authorization": "Bearer ${env:SPILLWAY_TOKEN}"}}}
    app = make_app()
    app.state.port = 9911
    async with make_client(app) as c:
        r = (await _get(c, "codex")).json()
    assert tomllib.loads(r["content"])["mcp_servers"]["spillway-all"]["url"] == "http://127.0.0.1:9911/mcp/all"


async def test_claude_code(client):
    body = (await _get(client, "claude-code")).json()
    v0, v1 = body["variants"]
    assert v0["format"] == "json" and v0["path"] == ".mcp.json" and v0["scope"] == "project"
    assert json.loads(v0["content"])["mcpServers"]["spillway-all"] == {
        "type": "http", "url": "http://127.0.0.1:8787/mcp/all",
        "headers": {"Authorization": "Bearer ${SPILLWAY_TOKEN}"}}
    assert v1["format"] == "command"
    assert shlex.split(v1["content"]) == [
        "claude", "mcp", "add", "--transport", "http", "spillway-all", "http://127.0.0.1:8787/mcp/all",
        "--header", "Authorization: Bearer ${SPILLWAY_TOKEN}"]
    assert "'Authorization: Bearer ${SPILLWAY_TOKEN}'" in v1["content"]
    assert any(n["severity"] == "warn" for n in body["notes"])


async def test_codex(client):
    v0, v1 = (await _get(client, "codex")).json()["variants"]
    t = tomllib.loads(v0["content"])["mcp_servers"]["spillway-all"]
    assert t == {"url": "http://127.0.0.1:8787/mcp/all", "bearer_token_env_var": "SPILLWAY_TOKEN",
                 "tool_timeout_sec": 600}
    assert "tool_timeout_sec = 600" in v0["content"]
    assert shlex.split(v1["content"]) == [
        "codex", "mcp", "add", "spillway-all", "--url", "http://127.0.0.1:8787/mcp/all",
        "--bearer-token-env-var", "SPILLWAY_TOKEN"]


async def test_gemini(client):
    body = (await _get(client, "gemini")).json()
    assert body["notes"][0]["severity"] == "warn" and "variants[1]" in body["notes"][0]["text"]
    v0, v1 = body["variants"]
    assert json.loads(v0["content"])["mcpServers"]["spillway-all"] == {
        "httpUrl": "http://127.0.0.1:8787/mcp/all",
        "headers": {"Authorization": "Bearer $SPILLWAY_TOKEN"}}
    s = json.loads(v1["content"])["mcpServers"]["spillway-all"]
    assert s["env"] == {"SPILLWAY_TOKEN": "$SPILLWAY_TOKEN"} and "url" not in s and "httpUrl" not in s
    assert s["args"][-5:] == ["mcp-stdio", "--group", "all", "--port", "8787"]


async def test_stdio_variants_and_absolute_command(client):
    body = (await _get(client, "stdio")).json()
    by = {v["label"].split("(")[0]: v for v in body["variants"]}
    assert set(by) == {"Claude Code", "Cursor", "Codex", "Gemini CLI"}
    cc = json.loads(by["Claude Code"]["content"])["mcpServers"]["spillway-all"]
    cu = json.loads(by["Cursor"]["content"])["mcpServers"]["spillway-all"]
    ge = json.loads(by["Gemini CLI"]["content"])["mcpServers"]["spillway-all"]
    co = tomllib.loads(by["Codex"]["content"])["mcp_servers"]["spillway-all"]
    assert cc["env"] == {"SPILLWAY_TOKEN": "${SPILLWAY_TOKEN}"} and cc["type"] == "stdio"
    assert cu["env"] == {"SPILLWAY_TOKEN": "${env:SPILLWAY_TOKEN}"}
    assert ge["env"] == {"SPILLWAY_TOKEN": "$SPILLWAY_TOKEN"}
    assert co["env_vars"] == ["SPILLWAY_TOKEN"] and "env" not in co
    for cfg in (cc, cu, ge, co):
        assert Path(cfg["command"]).is_absolute()
        assert cfg["args"][-5:] == ["mcp-stdio", "--group", "all", "--port", "8787"]
        assert "SPILLWAY_DEPTH" not in json.dumps(cfg)
    assert any("絕對路徑" in n["text"] for n in body["notes"])


def test_stdio_command_fallback(monkeypatch):
    monkeypatch.setattr(mcp_config, "shutil", SimpleNamespace(which=lambda _n: None))
    assert mcp_config.stdio_command() == (sys.executable, ["-m", "spillway"])
    ctx = build_context("g", 8787)
    assert ctx["args_prefix"] == ["-m", "spillway"]
    monkeypatch.setattr(mcp_config, "shutil", SimpleNamespace(which=lambda _n: "/opt/bin/spillway"))
    assert mcp_config.stdio_command() == ("/opt/bin/spillway", [])


HOSTILE = [EVIL, 'q"uote', "back\\slash", ".", "..", "a'b", "a$HOME`x`"]


@pytest.mark.parametrize("group", HOSTILE)
@pytest.mark.parametrize("tool", TOOLS)
async def test_hostile_group_name_stays_parseable(client, tool, group):
    await _mk_group(client, group)
    body = (await _get(client, tool, group)).json()
    url = f"http://127.0.0.1:8787/mcp/{quote(group, safe='')}"
    name = f"spillway-{group}"
    for v in body["variants"]:
        parsed = _parse(v)
        if v["format"] == "command":
            assert len(parsed) == (9 if tool == "claude-code" else 8)
            assert name in parsed and url in parsed
        else:
            servers = parsed["mcpServers" if v["format"] == "json" else "mcp_servers"]
            assert list(servers) == [name]
            srv = servers[name]
            if "args" in srv:
                assert srv["args"][-4:] == ["--group", group, "--port", "8787"]
            else:
                assert (srv.get("url") or srv.get("httpUrl")) == url


async def test_port_in_stdio_args_and_urls(make_app, make_client):
    app = make_app()
    app.state.port = 9911
    async with make_client(app) as c:
        for tool in TOOLS:
            for v in (await _get(c, tool)).json()["variants"]:
                p = _parse(v)
                if v["format"] == "command":
                    assert "http://127.0.0.1:9911/mcp/all" in p or "http://127.0.0.1:9911/mcp/all" in " ".join(p)
                    continue
                srv = next(iter(p["mcpServers" if v["format"] == "json" else "mcp_servers"].values()))
                if "args" in srv:
                    assert srv["args"][-4:] == ["--group", "all", "--port", "9911"]
                else:
                    assert (srv.get("url") or srv.get("httpUrl")) == "http://127.0.0.1:9911/mcp/all"


async def test_toml_escape_extremes():
    ctx = build_context('x\\y"z\n\t', 8787)
    for tool in ("codex",):
        for v in render_config(tool, ctx):
            if v["format"] == "toml":
                tomllib.loads(v["content"])


async def test_all_and_named_group_ok_unknown_404_bad_tool_422(client):
    assert (await _get(client, "codex", "all")).status_code == 200
    await _mk_group(client, "coding")
    assert (await _get(client, "codex", "coding")).status_code == 200
    r = await _get(client, "codex", "nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = await _get(client, "vim", "all")
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    r = await _get(client, "vim", "nope")     # 工具先於群組驗證
    assert r.status_code == 422
    r = await client.get("/api/mcp/config", params={"tool": "codex"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("tool", TOOLS)
async def test_no_token_in_any_variant(client, tool):
    token = (await client.post("/api/settings/token/rotate")).json()["token"]
    r = await _get(client, tool)
    assert token not in r.text
    for v in r.json()["variants"]:
        assert token not in v["content"]


# ---- L2:template_dir 注入 ---------------------------------------------------------------------

def test_template_dir_injection_new_tool(tmp_path, monkeypatch):
    (tmp_path / "newtool.j2").write_text('{"srv": {{ name|tojson }}, "u": {{ url|tojson }}}\n')
    monkeypatch.setitem(TOOL_FORMATS, "newtool", [
        VariantSpec("新工具", "user", "json", "newtool.j2", "~/.new/mcp.json", "mcp.json")])
    out = render_config("newtool", build_context("g", 1234), template_dir=tmp_path)
    assert len(out) == 1
    assert json.loads(out[0]["content"]) == {"srv": "spillway-g", "u": "http://127.0.0.1:1234/mcp/g"}


def test_missing_variable_is_an_error(tmp_path, monkeypatch):
    (tmp_path / "bad.j2").write_text("{{ nope }}")
    monkeypatch.setitem(TOOL_FORMATS, "bad", [VariantSpec("b", "user", "json", "bad.j2")])
    import jinja2
    with pytest.raises(jinja2.UndefinedError):
        render_config("bad", build_context("g", 1), template_dir=tmp_path)

"""全端點 Host/Origin 檢查(CHG-20261102-01 T1;決策 4,決策審議修訂 AM-8、AM-11)。

L1(行程內,直接以 raw ASGI scope 呼叫完整 app,才能構造重複 Host、無 Host 等 httpx 不會送出的 header):
Host 與 Origin 的放行/拒絕案例;拒絕是 403 不是 500;`/v1`、`/api`、`/mcp` 皆適用;錯誤 body 為
spec 9.1.2;websocket scope 被拒時送 `websocket.close`;lifespan 經過 middleware 正常啟動;
列舉全部 GET 路由(由 OpenAPI 取得)斷言不改變資料庫狀態(D33 不變式)。
L2:middleware 先於路由(未知路徑與 `/mcp/x` 回 403 而非 404);`_run_server` 把埠傳給 `create_app`
(`app.state.port`);子行程 serve 以非 8787 埠啟動:`Origin: http://127.0.0.1:8787` 回 403、同埠的
Origin 放行;HTTP/1.0 無 Host 經真實 TCP 回 403。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from spillway import cli
from spillway.app import create_app
from tests.helpers import dead_base, local_member

pytestmark = pytest.mark.anyio

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
PORT = 8787

ALLOWED_HOSTS = ["127.0.0.1:8787", "localhost:1234", "127.0.0.1", "LOCALHOST:8787", "localhost"]
REJECTED_HOSTS = ["evil.example:8787", "127.0.0.1.evil.example", "evil.localhost",
                  "localhost.evil.example", "127.0.0.1:8787:x", "[::1]:8787", "[::1]", "localhost.",
                  "", "127.0.0.1:123456", "0.0.0.0:8787", " 127.0.0.1"]
ALLOWED_ORIGINS = [f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}",
                   f"HTTP://LOCALHOST:{PORT}"]
REJECTED_ORIGINS = ["http://evil.example", "http://127.0.0.1:9999", "null", f"http://127.0.0.1:{PORT}/",
                    f"https://127.0.0.1:{PORT}", "http://127.0.0.1", f"http://localhost:{PORT}/x", ""]
PATHS = [("GET", "/v1/models"), ("POST", "/v1/chat/completions"), ("GET", "/api/settings/context"),
         ("POST", "/api/settings/token/rotate"), ("POST", "/mcp/coding"), ("GET", "/mcp/all"),
         ("DELETE", "/mcp/coding"), ("GET", "/no/such/path")]


# ---- raw ASGI helpers ------------------------------------------------------------------------

def _scope(method: str, path: str, headers: list[tuple[str, str]], *, kind: str = "http",
           http_version: str = "1.1") -> dict[str, Any]:
    return {
        "type": kind, "asgi": {"version": "3.0"}, "http_version": http_version, "method": method,
        "scheme": "http" if kind == "http" else "ws", "path": path, "raw_path": path.encode(),
        "root_path": "", "query_string": b"", "server": ("127.0.0.1", PORT),
        "client": ("127.0.0.1", 50000),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
    }


async def _call(app, scope: dict[str, Any], body: bytes = b"") -> tuple[int | None, Any, list[dict]]:
    sent: list[dict] = []
    first = True

    async def receive() -> dict:
        nonlocal first
        if scope["type"] == "websocket":
            if first:
                first = False
                return {"type": "websocket.connect"}
            return {"type": "websocket.disconnect", "code": 1000}
        if first:
            first = False
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(msg: dict) -> None:
        sent.append(msg)

    await app(scope, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    try:
        payload = json.loads(raw) if raw else None
    except ValueError:
        payload = raw
    return status, payload, sent


async def _req(app, method: str, path: str, headers: list[tuple[str, str]], **kw):
    body = b"{}" if method == "POST" else b""
    hs = list(headers) + ([("content-type", "application/json")] if method == "POST" else [])
    return await _call(app, _scope(method, path, hs, **kw), body)


def _assert_rejected(status, payload, code: str) -> None:
    assert status == 403, (status, payload)
    assert payload == {"error": {"code": code, "message": payload["error"]["message"]}}
    assert isinstance(payload["error"]["message"], str) and payload["error"]["message"]


@pytest.fixture
async def app(spillway):
    """lifespan 已進入的完整 app(含 middleware stack);呼叫 `app(scope, receive, send)`。"""
    return spillway.app


# ---- L1:Host ---------------------------------------------------------------------------------

@pytest.mark.parametrize("host", ALLOWED_HOSTS)
async def test_local_host_allowed(app, host):
    status, payload, _ = await _req(app, "GET", "/api/settings/context", [("host", host)])
    assert status == 200, payload


@pytest.mark.parametrize("host", REJECTED_HOSTS)
@pytest.mark.parametrize("method,path", PATHS)
async def test_non_local_host_rejected_on_every_endpoint(app, host, method, path):
    status, payload, _ = await _req(app, method, path, [("host", host)])
    _assert_rejected(status, payload, "host_not_allowed")


async def test_duplicate_host_rejected(app):
    status, payload, _ = await _req(app, "GET", "/api/settings/context",
                                    [("host", "127.0.0.1:8787"), ("host", "127.0.0.1:8787")])
    _assert_rejected(status, payload, "host_not_allowed")


@pytest.mark.parametrize("http_version", ["1.0", "1.1"])
async def test_missing_host_rejected(app, http_version):
    status, payload, _ = await _req(app, "GET", "/api/settings/context", [],
                                    http_version=http_version)
    _assert_rejected(status, payload, "host_not_allowed")


# ---- L1:Origin -------------------------------------------------------------------------------

LOCAL = [("host", f"127.0.0.1:{PORT}")]


async def test_no_origin_allowed(app):
    status, _, _ = await _req(app, "GET", "/api/settings/context", LOCAL)
    assert status == 200


@pytest.mark.parametrize("origin", ALLOWED_ORIGINS)
async def test_local_origin_on_bound_port_allowed(app, origin):
    status, payload, _ = await _req(app, "GET", "/api/settings/context", LOCAL + [("origin", origin)])
    assert status == 200, payload


@pytest.mark.parametrize("origin", REJECTED_ORIGINS)
@pytest.mark.parametrize("method,path", PATHS)
async def test_foreign_origin_rejected_on_every_endpoint(app, origin, method, path):
    status, payload, _ = await _req(app, method, path, LOCAL + [("origin", origin)])
    _assert_rejected(status, payload, "origin_not_allowed")


async def test_duplicate_origin_rejected(app):
    ok = f"http://127.0.0.1:{PORT}"
    status, payload, _ = await _req(app, "GET", "/api/settings/context",
                                    LOCAL + [("origin", ok), ("origin", ok)])
    _assert_rejected(status, payload, "origin_not_allowed")


async def test_host_checked_before_origin(app):
    status, payload, _ = await _req(app, "GET", "/v1/models",
                                    [("host", "evil.example"), ("origin", "http://evil.example")])
    _assert_rejected(status, payload, "host_not_allowed")


async def test_rejected_post_does_not_reach_endpoint(app, spillway):
    """跨站 POST rotate 被拒:403(不是 500)且 token 雜湊未變。"""
    from spillway import settings as st

    with spillway.db() as conn:
        st.ensure_access_token(conn)
        before = st.get_access_token_hash(conn)
    assert before is not None
    status, payload, _ = await _req(app, "POST", "/api/settings/token/rotate",
                                    LOCAL + [("origin", "http://evil.example")])
    _assert_rejected(status, payload, "origin_not_allowed")
    with spillway.db() as conn:
        assert st.get_access_token_hash(conn) == before


# ---- L1:websocket 與 lifespan ------------------------------------------------------------------

async def test_websocket_scope_rejected_with_close(app):
    _, _, sent = await _call(app, _scope("GET", "/mcp/all", [("host", "evil.example")],
                                         kind="websocket"))
    assert sent and sent[0]["type"] == "websocket.close"
    assert not any(m["type"].startswith("http.") for m in sent)


async def test_lifespan_passes_through_middleware(tmp_path):
    app = create_app(db_path=tmp_path / "ls.db", port=4321)
    msgs = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
    sent: list[dict] = []

    async def receive() -> dict:
        return next(msgs)

    async def send(msg: dict) -> None:
        sent.append(msg)

    await app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}, receive, send)
    assert [m["type"] for m in sent] == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert app.state.port == 4321


def test_create_app_default_port_is_8787(tmp_path):
    assert create_app(db_path=tmp_path / "d.db").state.port == 8787


# ---- L1:GET 不變式(D33) ---------------------------------------------------------------------

def _snapshot(db_path: Path) -> list[str]:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


async def test_all_get_routes_do_not_change_state(spillway):
    c = spillway.client
    spillway.add_deployment(local_member("loc-1", dead_base().removesuffix("/v1"), tags=("g",)))
    assert (await c.post("/api/groups", json={"name": "grp", "selector": {"all": ["g"]}})).status_code == 201
    assert (await c.post("/api/routes", json={"name": "rt", "target": {"group": "grp"}})).status_code == 201
    fill = {"dep_id": "loc-1", "name": "rt", "ledger_id": "1"}
    group_fill = {"name": "grp"}

    gets = [p for p, ops in spillway.app.openapi()["paths"].items() if "get" in ops]
    assert {"/v1/models", "/api/dashboard", "/api/ledger", "/api/settings/context"} <= set(gets)
    before = _snapshot(spillway.db_path)
    for path in gets:
        url = path.format(**(group_fill if path.startswith("/api/groups/") else fill))
        r = await c.get(url)
        assert r.status_code < 500, (url, r.status_code, r.text)
    assert _snapshot(spillway.db_path) == before


# ---- L2:middleware 先於路由;埠的傳遞 ----------------------------------------------------------

@pytest.mark.parametrize("path", ["/no/such/path", "/mcp/x", "/mcp"])
async def test_middleware_runs_before_routing(app, path):
    status, payload, _ = await _req(app, "GET", path, [("host", "evil.example")])
    _assert_rejected(status, payload, "host_not_allowed")
    status, payload, _ = await _req(app, "GET", path, LOCAL)
    assert status == 404


async def test_origin_compared_with_create_app_port(tmp_path):
    app = create_app(db_path=tmp_path / "p.db", port=4321)
    async with app.router.lifespan_context(app):
        status, payload, _ = await _req(app, "GET", "/api/settings/context",
                                        LOCAL + [("origin", "http://127.0.0.1:8787")])
        _assert_rejected(status, payload, "origin_not_allowed")
        status, _, _ = await _req(app, "GET", "/api/settings/context",
                                  LOCAL + [("origin", "http://localhost:4321")])
        assert status == 200


class _Captured(Exception):
    pass


def test_run_server_passes_port_to_create_app(monkeypatch):
    import uvicorn

    seen: dict[str, Any] = {}

    def fake_run(self, sockets=None):
        seen["server"] = self
        raise _Captured

    monkeypatch.setattr(cli, "_preflight", lambda *a, **k: 0)
    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    with pytest.raises(_Captured):
        cli._run_server("127.0.0.1", 9123)
    assert seen["server"].config.app.state.port == 9123


# ---- L2:子行程 serve(非 8787 埠) --------------------------------------------------------------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _raw_http10(port: int, path: str) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(f"GET {path} HTTP/1.0\r\n\r\n".encode())
        chunks = []
        while data := s.recv(65536):
            chunks.append(data)
    return b"".join(chunks)


def test_subprocess_serve_checks_origin_against_bound_port(tmp_path):
    port = _free_port()
    while port == PORT:
        port = _free_port()
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items()
           if k not in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    env.update(SPILLWAY_HOME=str(home), PYTHONPATH=str(SRC), LITELLM_LOCAL_MODEL_COST_MAP="True")
    p = subprocess.Popen([sys.executable, "-m", "spillway", "serve", "--port", str(port)], env=env,
                         cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    url = f"http://127.0.0.1:{port}/api/settings/context"
    try:
        deadline = time.monotonic() + 60
        while True:
            if p.poll() is not None:
                pytest.fail(f"serve 提前結束 rc={p.returncode}\n{p.communicate()[1]}")
            try:
                if httpx.get(url, timeout=2).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                pytest.fail("serve 未在 60 秒內就緒")
            time.sleep(0.2)

        r = httpx.get(url, headers={"Origin": f"http://127.0.0.1:{PORT}"}, timeout=10)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "origin_not_allowed"
        for origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            assert httpx.get(url, headers={"Origin": origin}, timeout=10).status_code == 200
        r = httpx.get(url, headers={"Host": "evil.example"}, timeout=10)
        assert r.status_code == 403 and r.json()["error"]["code"] == "host_not_allowed"
        raw = _raw_http10(port, "/api/settings/context")
        head, _, body = raw.partition(b"\r\n\r\n")
        assert head.split(b" ", 2)[1] == b"403", raw[:200]
        assert json.loads(body)["error"]["code"] == "host_not_allowed"
    finally:
        if p.poll() is None:
            p.terminate()
            try:
                p.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()
                p.communicate()

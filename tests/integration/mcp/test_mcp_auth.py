"""`/mcp/<group>` 的驗證順序與錯誤碼(CHG-20261102-01 T2 L1/L2;決策 9、10;AM-4、AM-10、AM-11)。

HTTP 層的 401/403/404/503 以 httpx `ASGITransport` 打行程內 app(`asgi_app`;回應不是串流,緩衝無妨),
斷言原生 HTTP body(spec 9.1.2)。session 跨群組與 lifespan 進出另見各測試說明。
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from spillway import settings
from spillway.app import create_app
from spillway.clock import SystemRng
from spillway.store import repo
from spillway.store.db import connect
from tests.integration.mcp.conftest import INIT_BODY, MCP_ACCEPT

pytestmark = pytest.mark.anyio


def _hdrs(token: str | None = None, **extra: str) -> dict[str, str]:
    h = {"Accept": MCP_ACCEPT, "Content-Type": "application/json"}
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    h.update(extra)
    return h


def _assert_error(r: httpx.Response, status: int, code: str) -> None:
    assert r.status_code == status, (r.status_code, r.text)
    assert r.json() == {"error": {"code": code, "message": r.json()["error"]["message"]}}
    assert isinstance(r.json()["error"]["message"], str) and r.json()["error"]["message"]


def _sse_json(r: httpx.Response) -> dict:
    """POST 的 SSE 回應 → 第一個 JSON-RPC 訊息。"""
    for line in r.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(f"沒有 SSE data:{r.text!r}")


async def _init(client: httpx.AsyncClient, path: str, token: str) -> httpx.Response:
    return await client.post(path, json=INIT_BODY, headers=_hdrs(token))


# ---- ② depth ---------------------------------------------------------------------------------

@pytest.mark.parametrize("values", [["1"], ["-1"], ["+1"], [""], ["abc"], [" 0"], ["0", "1"], ["2", "0"],
                                    ["01"]])
async def test_depth_rejected_before_token(asgi_app, values):
    raw = [(b"x-spillway-depth", v.encode()) for v in values]
    for token in (asgi_app.token, None, "wrong"):
        headers = [(k.lower().encode(), v.encode()) for k, v in _hdrs(token).items()] + raw
        r = await asgi_app.client.post("/mcp/all", json=INIT_BODY, headers=headers)
        _assert_error(r, 403, "recursion_blocked")   # `01` 為數值 1(前導零)仍 ≥ 1


@pytest.mark.parametrize("value", ["1" * 5000, "0" * 4999 + "1"])
async def test_huge_depth_is_403_not_500(asgi_app, value):
    """超過 Python 整數字串長度上限(4300 位數)的值:不可因 int() 的 ValueError 變成 500。"""
    r = await asgi_app.client.post("/mcp/all", json=INIT_BODY,
                                   headers=_hdrs(asgi_app.token, **{"X-Spillway-Depth": value}))
    _assert_error(r, 403, "recursion_blocked")


async def test_huge_zero_depth_passes(asgi_app):
    r = await asgi_app.client.post("/mcp/all", json=INIT_BODY,
                                   headers=_hdrs(asgi_app.token, **{"X-Spillway-Depth": "0" * 5000}))
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("method", ["GET", "DELETE"])
@pytest.mark.parametrize("value", ["1", "-1", "abc"])
async def test_depth_rejected_on_get_and_delete(asgi_app, method, value):
    for token in (asgi_app.token, None):
        r = await asgi_app.client.request(method, "/mcp/all",
                                          headers=_hdrs(token, **{"X-Spillway-Depth": value}))
        _assert_error(r, 403, "recursion_blocked")


@pytest.mark.parametrize("values", [["0"], ["0", "0"], ["00"]])
async def test_depth_zero_passes(asgi_app, values):
    raw = [(b"x-spillway-depth", v.encode()) for v in values]
    headers = [(k.lower().encode(), v.encode()) for k, v in _hdrs(asgi_app.token).items()] + raw
    r = await asgi_app.client.post("/mcp/all", json=INIT_BODY, headers=headers)
    assert r.status_code == 200, r.text


# ---- ③ Bearer --------------------------------------------------------------------------------

@pytest.mark.parametrize("auth", [None, "Bearer", "Bearer ", "Basic abc", "Token x", "Bearer wrong-token"])
async def test_bad_or_missing_token_401(asgi_app, auth):
    headers = _hdrs()
    if auth is not None:
        headers["Authorization"] = auth
    r = await asgi_app.client.post("/mcp/all", json=INIT_BODY, headers=headers)
    _assert_error(r, 401, "unauthorized")
    assert r.headers["www-authenticate"] == "Bearer"   # 不帶 resource_metadata


@pytest.mark.parametrize("scheme", ["bearer", "BEARER", "Bearer"])
async def test_scheme_case_insensitive(asgi_app, scheme):
    r = await asgi_app.client.post("/mcp/all", json=INIT_BODY,
                                   headers={**_hdrs(), "Authorization": f"{scheme} {asgi_app.token}"})
    assert r.status_code == 200, r.text
    assert r.headers.get("mcp-session-id")


async def test_duplicate_authorization_header_401(asgi_app):
    good = f"Bearer {asgi_app.token}".encode()
    headers = [(b"accept", MCP_ACCEPT.encode()), (b"content-type", b"application/json"),
               (b"authorization", good), (b"authorization", good)]
    r = await asgi_app.client.post("/mcp/all", json=INIT_BODY, headers=headers)
    _assert_error(r, 401, "unauthorized")


@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_get_and_delete_without_token_401(asgi_app, method):
    r = await asgi_app.client.request(method, "/mcp/all", headers={"Accept": MCP_ACCEPT})
    _assert_error(r, 401, "unauthorized")
    r = await asgi_app.client.request(method, "/mcp/coding", headers={"Accept": MCP_ACCEPT})
    _assert_error(r, 401, "unauthorized")   # 先驗 token,不洩漏群組是否存在


async def test_rotate_invalidates_old_token_immediately(asgi_app):
    old = asgi_app.token
    assert (await _init(asgi_app.client, "/mcp/all", old)).status_code == 200
    r = await asgi_app.client.post("/api/settings/token/rotate")
    assert r.status_code == 200, r.text
    new = r.json()["token"]
    _assert_error(await _init(asgi_app.client, "/mcp/all", old), 401, "unauthorized")
    assert (await _init(asgi_app.client, "/mcp/all", new)).status_code == 200


async def test_no_token_generated_yet_401(fake_key_env, clock, tmp_path):
    app = create_app(clock=clock, rng=SystemRng(seed=0), db_path=tmp_path / "fresh.db",
                     retention_poll_seconds=3600)
    async with app.router.lifespan_context(app):
        conn = app.state.conn_factory()
        try:
            assert settings.get_access_token_hash(conn) is None
        finally:
            conn.close()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:8787") as c:
            for tok in ("", "anything", settings.generate_access_token()):
                r = await c.post("/mcp/all", json=INIT_BODY,
                                 headers={**_hdrs(), "Authorization": f"Bearer {tok}"})
                _assert_error(r, 401, "unauthorized")


# ---- ④ 停機與 lifespan 外 ----------------------------------------------------------------------

async def test_shutting_down_503(asgi_app):
    asgi_app.app.state.shutting_down = True
    try:
        for method in ("POST", "GET", "DELETE"):
            r = await asgi_app.client.request(method, "/mcp/all", json=INIT_BODY if method == "POST" else None,
                                              headers=_hdrs(asgi_app.token))
            _assert_error(r, 503, "shutting_down")
        # 驗證順序:token 錯仍是 401(③ 先於 ④)
        _assert_error(await _init(asgi_app.client, "/mcp/all", "wrong"), 401, "unauthorized")
    finally:
        asgi_app.app.state.shutting_down = False


async def test_outside_lifespan_503(fake_key_env, clock, mcp_db_path):
    conn = connect(mcp_db_path)
    try:
        token = settings.rotate_access_token(conn)
    finally:
        conn.close()
    app = create_app(clock=clock, rng=SystemRng(seed=0), db_path=mcp_db_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8787") as c:
        _assert_error(await _init(c, "/mcp/all", token), 503, "shutting_down")
        async with app.router.lifespan_context(app):
            assert (await _init(c, "/mcp/all", token)).status_code == 200
        _assert_error(await _init(c, "/mcp/all", token), 503, "shutting_down")   # 離開後


# ---- ⑤ 群組 ------------------------------------------------------------------------------------

async def test_group_existence(asgi_app, db):
    repo.save_group(db, repo.Group("coding", {"all": ["a"]}))
    db.commit()
    assert (await _init(asgi_app.client, "/mcp/all", asgi_app.token)).status_code == 200
    assert (await _init(asgi_app.client, "/mcp/coding", asgi_app.token)).status_code == 200
    for path in ("/mcp/nope", "/mcp/coding/x", "/mcp/", "/mcp/Coding"):
        _assert_error(await _init(asgi_app.client, path, asgi_app.token), 404, "not_found")


async def test_check_order(asgi_app):
    # ① Host 先於 ② depth
    r = await asgi_app.client.post("/mcp/all", json=INIT_BODY,
                                   headers={**_hdrs(), "Host": "evil.example", "X-Spillway-Depth": "1"})
    _assert_error(r, 403, "host_not_allowed")
    # ② depth 先於 ③ token
    r = await asgi_app.client.post("/mcp/nope", json=INIT_BODY, headers=_hdrs(None, **{"X-Spillway-Depth": "1"}))
    _assert_error(r, 403, "recursion_blocked")
    # ③ token 先於 ⑤ 群組
    _assert_error(await _init(asgi_app.client, "/mcp/nope", "wrong"), 401, "unauthorized")


async def test_session_bound_to_group(asgi_app, db):
    """同一 session id 改打別的群組 → SDK 回 404(body 為 SDK 的 JSON-RPC 格式,例外於 9.1.2)。"""
    repo.save_group(db, repo.Group("coding", {"all": ["a"]}))
    repo.save_group(db, repo.Group("ops", {"all": ["b"]}))
    db.commit()
    c, tok = asgi_app.client, asgi_app.token
    r = await _init(c, "/mcp/coding", tok)
    assert r.status_code == 200, r.text
    sid = r.headers["mcp-session-id"]
    h = {**_hdrs(tok), "Mcp-Session-Id": sid, "Mcp-Protocol-Version": "2025-06-18"}
    r = await c.post("/mcp/coding", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
    assert r.status_code == 202, r.text
    list_body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    r = await c.post("/mcp/coding", json=list_body, headers=h)
    assert r.status_code == 200 and "ask" in r.text
    for other in ("/mcp/ops", "/mcp/all"):
        r = await c.post(other, json=list_body, headers=h)
        assert r.status_code == 404, r.text
        assert r.json()["jsonrpc"] == "2.0"   # SDK 的 JSON-RPC 格式
        assert r.json()["error"]["message"] == "Session not found"
    r = await c.post("/mcp/coding", json=list_body, headers=h)   # 原群組仍可用
    assert r.status_code == 200


async def test_token_never_logged(asgi_app, caplog):
    caplog.set_level(logging.DEBUG)
    await _init(asgi_app.client, "/mcp/all", asgi_app.token)
    await _init(asgi_app.client, "/mcp/all", "wrong-token-value-123")
    await asyncio.sleep(0)
    assert asgi_app.token not in caplog.text
    assert "wrong-token-value-123" not in caplog.text


# ---- lifespan 兩次進出(AM-4)-------------------------------------------------------------------

async def test_same_app_two_lifespans(fake_key_env, clock, mcp_db_path):
    conn = connect(mcp_db_path)
    try:
        token = settings.rotate_access_token(conn)
    finally:
        conn.close()
    app = create_app(clock=clock, rng=SystemRng(seed=0), db_path=mcp_db_path)
    managers = []
    for _ in range(2):
        async with app.router.lifespan_context(app):
            managers.append(app.state.mcp_manager)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://127.0.0.1:8787") as c:
                r = await _init(c, "/mcp/all", token)
                assert r.status_code == 200, r.text
                assert _sse_json(r)["result"]["capabilities"]["tools"]["listChanged"] is True
        assert app.state.mcp_manager is None
    assert managers[0] is not managers[1]


def test_lifespan_enter_exit_in_separate_runner_runs(fake_key_env, clock, mcp_db_path):
    """behave `Gateway` 的方式:`asyncio.Runner` 分兩次 `run()` 進出 lifespan,不丟例外。"""
    conn = connect(mcp_db_path)
    try:
        token = settings.rotate_access_token(conn)
    finally:
        conn.close()
    app = create_app(clock=clock, rng=SystemRng(seed=0), db_path=mcp_db_path)
    lifespan = app.router.lifespan_context(app)
    with asyncio.Runner() as runner:
        runner.run(lifespan.__aenter__())
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787")
        r = runner.run(_init(client, "/mcp/all", token))
        assert r.status_code == 200, r.text
        runner.run(client.aclose())
        runner.run(lifespan.__aexit__(None, None, None))
    assert app.state.mcp_manager is None

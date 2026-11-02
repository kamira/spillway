"""tests/integration/mcp 共用 fixture(CHG-20261102-01 T2;AM-5)。

兩種連線方式,依測試需要選擇:

- `live_gateway`(預設用這個):`tests/helpers/live_gateway.py` 的背景 thread `uvicorn.Server`,127.0.0.1
  隨機埠、`create_app(port=同一埠)`、已產生 token(`gw.token`)。SDK 用戶端(`gw.mcp_session(...)`)以真實
  HTTP 連線,GET SSE 通知串流不會被緩衝。**凡需要 GET SSE、`list_changed`、stdio 或停機的測試一律用它**。
- `asgi_app`:行程內 `create_app()` + 手動進出 lifespan,以 httpx `ASGITransport` 直接打 ASGI。
  **只用於 HTTP 層的 401/403/404/503 與單次 POST**:`ASGITransport` 會把整個回應緩衝完才交回,GET SSE
  串流在這種傳輸下永遠拿不到事件(也不會結束)。斷言 401/403/404 的 HTTP body 一律用原生 httpx,
  不用 SDK 用戶端(SDK 遇非 2xx 只丟 `HTTPStatusError`)。

共用:`fake_key_env`(假金鑰)、`fake_api`(假上游);clock 為根 conftest 的 `FakeClock`。
`seed_basic(gw_or_db_path, base_url)`:建立兩個群組與四個 route(見函式說明)。
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from spillway import settings
from spillway.app import create_app
from spillway.clock import FakeClock, SystemRng
from spillway.store import repo
from spillway.store.db import connect
from tests.helpers import api_member
from tests.helpers.live_gateway import LiveGateway, start_live_gateway

# MCP 的 POST 需要的 Accept(Streamable HTTP)
MCP_ACCEPT = "application/json, text/event-stream"
INIT_BODY = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "spillway-test", "version": "0"}}}


def seed_basic(db_path: Path, base_url: str) -> None:
    """成員 `api-a`(tag a)、`api-b`(tag b)、`api-ab`(tags a、b);群組 `coding`(all: [a])、
    `ops`(all: [b]);route `review`(group coding)、`review-b`(group coding)、`deploy`(group ops)、
    `chat-x`(tags a,不屬任何群組)。"""
    conn = connect(db_path)
    try:
        repo.save_deployment(conn, api_member("api-a", base_url, model="gpt-4o-mini", tags=("a",)))
        repo.save_deployment(conn, api_member("api-b", base_url, model="gpt-4.1-mini", tags=("b",)))
        repo.save_deployment(conn, api_member("api-ab", base_url, model="gpt-4.1-nano", tags=("a", "b")))
        repo.save_group(conn, repo.Group("coding", {"all": ["a"], "any": [], "none": []}))
        repo.save_group(conn, repo.Group("ops", {"all": ["b"], "any": [], "none": []}))
        repo.save_route(conn, repo.Route("review", {"group": "coding"}))
        repo.save_route(conn, repo.Route("review-b", {"group": "coding"}))
        repo.save_route(conn, repo.Route("deploy", {"group": "ops"}))
        repo.save_route(conn, repo.Route("chat-x", {"tags": {"all": ["a"]}}))
    finally:
        conn.close()


@pytest.fixture
def mcp_db_path() -> Path:
    return settings.db_path()


@pytest.fixture
def start_gateway(fake_key_env: str, fake_api: Any, clock: FakeClock, mcp_db_path: Path):
    """工廠:`start_gateway(**kw) -> LiveGateway`(測試結束時自動停止)。`kw` 傳給 `start_live_gateway`;
    預設 clock 為 `FakeClock`、rng 為 `SystemRng(seed=0)`、db 為暫存 `SPILLWAY_HOME` 的資料庫。"""
    started: list[LiveGateway] = []

    def _start(**kw: Any) -> LiveGateway:
        kw.setdefault("clock", clock)
        kw.setdefault("rng", SystemRng(seed=0))
        gw = start_live_gateway(mcp_db_path, **kw)
        started.append(gw)
        return gw

    yield _start
    for gw in started:
        gw.stop()


@pytest.fixture
def live_gateway(start_gateway) -> LiveGateway:
    return start_gateway()


@pytest.fixture
def seeded_gateway(start_gateway, fake_api: Any, mcp_db_path: Path) -> LiveGateway:
    gw = start_gateway()
    seed_basic(mcp_db_path, fake_api.base_url)
    return gw


@contextlib.asynccontextmanager
async def _asgi(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as c:
            yield c


@pytest.fixture
async def asgi_app(fake_key_env: str, clock: FakeClock, mcp_db_path: Path) -> AsyncIterator[Any]:
    """行程內 app(已進 lifespan,已產生 token),yield `SimpleNamespace(app, client, token)`:`client` 為
    `ASGITransport` 的 `httpx.AsyncClient`、`token` 為明文。只用於 HTTP 層的斷言(見模組說明)。"""
    conn = connect(mcp_db_path)
    try:
        token = settings.rotate_access_token(conn)
    finally:
        conn.close()
    app = create_app(clock=clock, rng=SystemRng(seed=0), db_path=mcp_db_path,
                     retention_poll_seconds=3600)
    async with _asgi(app) as client:
        yield SimpleNamespace(app=app, client=client, token=token)


@pytest.fixture
def db(mcp_db_path: Path) -> Iterator[Any]:
    conn = connect(mcp_db_path)
    try:
        yield conn
    finally:
        conn.close()

"""tests/integration/admin 共用 fixture(CHG-20261030-02 T14)。

不依賴 T13 的 `app.py`:以 `FastAPI()` 掛載 `admin.router`,自行設定 `app.state`
(`registry`、`router`、`conn_factory`、`clock`——與 T13 `create_app` 放的屬性名一致)。

- `make_app(extra_paths=(), rng=None)`:組一個 app;`extra_paths` 給 `AdapterRegistry.discover()`。
- `client`:預設 app 的 httpx `AsyncClient`(`ASGITransport`)。
- `make_client(app)`:為自訂 app 開 client(async context manager)。
- `db`:同一個暫存資料庫的直接連線,供測試準備資料與斷言。
- 自動設定 `SPILLWAY_TEST_KEY`(假金鑰)與 `SPILLWAY_LOCAL_API_BASE`(指向假上游,決策 25)。
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from spillway import credentials
from spillway.adapters.base import AdapterContext, AdapterRegistry
from spillway.api import admin
from spillway.clock import FakeClock, SeqRng
from spillway.routing import Router
from spillway.store import db as store_db

from .helpers import KEY


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch: pytest.MonkeyPatch, fake_api: Any) -> None:
    monkeypatch.setenv("SPILLWAY_TEST_KEY", KEY)
    monkeypatch.setenv("SPILLWAY_LOCAL_API_BASE", fake_api.base_url + "/v1")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "admin.db"


@pytest.fixture
def db(db_path: Path) -> Iterator[Any]:
    conn = store_db.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def make_app(db_path: Path, clock: FakeClock):
    def _make(extra_paths: tuple[Path, ...] | list[Path] = (), rng: Any = None) -> FastAPI:
        registry = AdapterRegistry(AdapterContext(credentials.resolve, clock))
        registry.discover(extra_paths)

        def conn_factory():
            return store_db.connect(db_path)

        app = FastAPI()
        app.include_router(admin.router)
        app.state.registry = registry
        app.state.conn_factory = conn_factory
        app.state.clock = clock
        app.state.router = Router(conn_factory, registry, clock, rng or SeqRng([0.0]),
                                  close_conn=True)
        return app

    return _make


@contextlib.asynccontextmanager
async def _client_for(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def make_client():
    return _client_for


@pytest.fixture
async def client(make_app) -> AsyncIterator[httpx.AsyncClient]:
    async with _client_for(make_app()) as c:
        yield c

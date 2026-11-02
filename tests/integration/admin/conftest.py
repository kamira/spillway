"""tests/integration/admin 共用 fixture(CHG-20261030-02 T14;CHG-20261101-02 T11 決策 19 改用 `create_app()`)。

`make_app` 呼叫 `spillway.app.create_app()`,不再手抄 `app.state` 接線。`registry`、`router`、
`retention` 在 lifespan 啟動後才存在;`make_client(app)` / `client` 以 `app.router.lifespan_context(app)`
進出 lifespan(httpx `ASGITransport` 不送 lifespan 事件),與 `tests/integration/conftest.py` 的
`open_spillway` 同一方式。lifespan 結束後 `app.state` 的屬性仍保留,可在 client 關閉後直接呼叫 handler。

- `make_app(extra_paths=(), rng=None)`:組一個 app;`extra_paths` 給 `AdapterRegistry.discover()`;
  `rng` 預設 `SeqRng([0.0])`;clock 為根 conftest 的 `FakeClock`;資料庫為 `db_path`。
- `client`:預設 app 的 httpx `AsyncClient`(`ASGITransport`)。
- `make_client(app)`:為自訂 app 進 lifespan 並開 client(async context manager)。
- `db`:同一個暫存資料庫的直接連線,供測試準備資料與斷言。
- 自動設定 `SPILLWAY_TEST_KEY`(假金鑰,`tests/helpers.py` 的 `fake_key_env`)與
  `SPILLWAY_LOCAL_API_BASE`(指向假上游,CHG-20261030-02 決策 25)。
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from spillway.app import create_app
from spillway.clock import FakeClock, SeqRng
from spillway.store import db as store_db


@pytest.fixture(autouse=True)
def _admin_env(fake_key_env: str, monkeypatch: pytest.MonkeyPatch, fake_api: Any) -> None:
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
        return create_app(clock=clock, rng=rng or SeqRng([0.0]),
                          extra_adapter_paths=tuple(extra_paths), db_path=db_path)

    return _make


@contextlib.asynccontextmanager
async def _client_for(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as c:
            yield c


@pytest.fixture
def make_client():
    return _client_for


@pytest.fixture
async def client(make_app) -> AsyncIterator[httpx.AsyncClient]:
    async with _client_for(make_app()) as c:
        yield c

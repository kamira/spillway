"""tests/integration 共用 fixture(CHG-20261030-02 T13 擁有;T14、T15 共用)。

以 httpx `ASGITransport` 打 `spillway.app.create_app()` 組出的 app。`ASGITransport` 不送 lifespan
事件,所以這裡以 `app.router.lifespan_context(app)` 手動進出 lifespan(migration、adapter 發現、
RetentionJob 排程都在其中)。資料庫位於根 conftest 設定的暫存 `SPILLWAY_HOME`。

fixture:

- `open_spillway`:工廠,`async with open_spillway(clock=..., rng=..., extra_adapter_paths=...,
  db_path=..., retention_poll_seconds=...) as h:` 取得 `SpillwayHarness`;未給 clock 時用根 conftest
  的 `clock`(`FakeClock`),未給 rng 時用 `SystemRng(seed=0)`。其餘關鍵字原樣傳給 `create_app()`。
- `spillway`:以預設參數開好的 `SpillwayHarness`(最常用)。

`SpillwayHarness` 欄位:`app`、`client`(`httpx.AsyncClient`,base_url `http://spillway.test`)、
`clock`、`rng`、`home`(SPILLWAY_HOME)、`db_path`;方法:`db()`(context manager,開一條新連線、
離開時關閉)、`add_deployment(dep)`、`add_route(route, targets=())`、`ledger()`(新到舊的帳本列)。

成員建構器 `api_member`、`local_member` 與 `TEST_KEY`、`TEST_KEY_ENV`、`fake_key_env` 在
`tests/helpers.py`(CHG-20261101-02 T11);`anyio_backend` 在根 conftest。
"""
from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from spillway import settings
from spillway.app import create_app
from spillway.clock import Clock, FakeClock, Rng, SystemRng
from spillway.domain import Deployment
from spillway.store import repo
from spillway.store.db import connect

BASE_URL = "http://spillway.test"


@dataclass
class SpillwayHarness:
    app: Any
    client: httpx.AsyncClient
    clock: Clock
    rng: Rng
    home: Path
    db_path: Path

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def add_deployment(self, dep: Deployment) -> Deployment:
        with self.db() as conn:
            repo.save_deployment(conn, dep)
        return dep

    def add_route(self, route: repo.Route, targets: Sequence[repo.RouteTarget] = ()) -> repo.Route:
        with self.db() as conn:
            repo.save_route(conn, route)
            if targets:
                repo.replace_route_targets(conn, route.name, list(targets))
        return route

    def ledger(self, **filters: Any) -> list[repo.LedgerRow]:
        with self.db() as conn:
            return repo.list_ledger(conn, **filters)


OpenSpillway = Callable[..., Any]


@pytest.fixture
def open_spillway(clock: FakeClock) -> OpenSpillway:
    default_clock = clock

    @asynccontextmanager
    async def _open(
        *,
        clock: Clock | None = None,
        rng: Rng | None = None,
        extra_adapter_paths: Sequence[Path | str] = (),
        db_path: Path | None = None,
        **kw: Any,
    ) -> AsyncIterator[SpillwayHarness]:
        clk = clock or default_clock
        rnd = rng or SystemRng(seed=0)
        path = Path(db_path) if db_path is not None else settings.db_path()
        app = create_app(clock=clk, rng=rnd, extra_adapter_paths=tuple(extra_adapter_paths),
                         db_path=path, **kw)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
                yield SpillwayHarness(app, client, clk, rnd, settings.data_dir(), path)

    return _open


@pytest.fixture
async def spillway(open_spillway: OpenSpillway) -> AsyncIterator[SpillwayHarness]:
    async with open_spillway() as h:
        yield h

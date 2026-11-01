"""CHG-20261101-02 T11 審議修正:`_AdminRoute` 不靠 app 層 handler 就輸出 9.1.2 錯誤格式。

T11 起 `make_app` 改用 `create_app()`(含 app 層 error handler),其他 admin 測試不再覆蓋
「`admin.router` 掛到沒有任何 handler 的 `FastAPI()`」的情況;本檔補上。少了 `_AdminRoute` 時,
FastAPI 預設回 `{"detail": ...}`,`assert_error` 的 `set(body) == {"error"}` 會失敗。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI

from spillway.api import admin
from spillway.store import db as store_db

from .helpers import assert_error

pytestmark = pytest.mark.anyio


@pytest.fixture
def bare_app(db_path: Path) -> FastAPI:
    app = FastAPI()                       # 不呼叫 install_error_handlers
    app.include_router(admin.router)
    app.state.conn_factory = lambda: store_db.connect(db_path)
    return app


async def test_validation_error_formatted_without_app_handlers(bare_app, make_client):
    async with make_client(bare_app) as c:
        r = await c.post("/api/groups", json={"members": "not-a-list"})
    assert_error(r, 422, "validation_error")


async def test_not_found_formatted_without_app_handlers(bare_app, make_client):
    async with make_client(bare_app) as c:
        r = await c.get("/api/groups/nope")
    assert assert_error(r, 404, "not_found") == "找不到群組 nope"

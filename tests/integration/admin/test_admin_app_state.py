"""CHG-20261101-02 T11 L2:admin 測試的 `make_app` 改用 `create_app()`(決策 19)。

整併前 `make_app` 手抄的 `app.state` 屬性為 `HAND_COPIED`(T11 開工時擷取)。改用 `create_app()` 後,
這些屬性名稱全部仍在、型別與接線相同;`create_app()` 另外放的 `rng`、`db_path`、`retention`
(app.py docstring 列出的屬性)明列於 `CREATE_APP_EXTRA`,不多不少。
"""
from __future__ import annotations

import pytest

from spillway.adapters.base import AdapterRegistry
from spillway.app import RetentionScheduler
from spillway.routing import Router

pytestmark = pytest.mark.anyio

HAND_COPIED = frozenset({"registry", "router", "conn_factory", "clock"})
CREATE_APP_EXTRA = frozenset({"rng", "db_path", "retention"})


def _state_names(app) -> set[str]:
    return set(vars(app.state)["_state"])


async def test_make_app_state_names_cover_hand_copied_version(make_app, make_client, clock,
                                                              db_path):
    app = make_app()
    async with make_client(app):
        names = _state_names(app)
    assert HAND_COPIED <= names
    assert names - HAND_COPIED == CREATE_APP_EXTRA
    assert _state_names(app) == names          # lifespan 結束後屬性仍在
    assert isinstance(app.state.registry, AdapterRegistry)
    assert isinstance(app.state.router, Router)
    assert isinstance(app.state.retention, RetentionScheduler)
    assert app.state.clock is clock
    assert app.state.db_path == db_path
    conn = app.state.conn_factory()
    try:
        assert conn.execute("PRAGMA database_list").fetchone()[2] == str(db_path)
    finally:
        conn.close()

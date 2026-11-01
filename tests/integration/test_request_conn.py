"""CHG-20261101-02 T2 L1/L2:`api.deps.request_conn(request)`(決策 5)。

- 每次呼叫讀 `request.app.state.conn_factory`(建 app 後替換工廠立即生效),開一條連線、
  離開時關閉(含例外);不是 FastAPI `Depends`,handler 簽名不變。
- 管理 API 與 `/v1/models` 每請求開一條、關一條。
"""
from __future__ import annotations

import sqlite3
import threading
from types import SimpleNamespace

import pytest

from spillway.api import admin
from spillway.api.deps import request_conn
from spillway.store.db import connect

from .conftest import local_member

pytestmark = pytest.mark.anyio


def _alive(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return False
    return True


class _Counting:
    """包裝 conn_factory:記錄開出的每條連線與開啟所在的執行緒。"""

    def __init__(self, inner):
        self.inner = inner
        self.opened: list[sqlite3.Connection] = []
        self.threads: list[int] = []

    def __call__(self) -> sqlite3.Connection:
        c = self.inner()
        self.opened.append(c)
        self.threads.append(threading.get_ident())
        return c

    def closed_count(self) -> int:
        return sum(1 for c in self.opened if not _alive(c))


def _fake_request(factory) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(conn_factory=factory)))


# ---------------------------------------------------------------- L1


def test_request_conn_opens_one_and_closes_on_exit(tmp_path):
    f = _Counting(lambda: connect(tmp_path / "a.db"))
    req = _fake_request(f)
    with request_conn(req) as conn:
        assert f.opened == [conn]
        assert _alive(conn)
    assert len(f.opened) == 1 and not _alive(conn)


def test_request_conn_closes_on_exception(tmp_path):
    f = _Counting(lambda: connect(tmp_path / "a.db"))
    with pytest.raises(RuntimeError):
        with request_conn(_fake_request(f)):
            raise RuntimeError("boom")
    assert f.closed_count() == 1


def test_request_conn_reads_factory_on_every_call(tmp_path):
    first = _Counting(lambda: connect(tmp_path / "a.db"))
    second = _Counting(lambda: connect(tmp_path / "a.db"))
    req = _fake_request(first)
    with request_conn(req):
        pass
    req.app.state.conn_factory = second
    with request_conn(req):
        pass
    assert len(first.opened) == 1 and len(second.opened) == 1


def test_request_conn_factory_error_propagates(tmp_path):
    def broken():
        raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        with request_conn(_fake_request(broken)):
            pass  # pragma: no cover


# ---------------------------------------------------------------- L2


async def test_admin_get_50_times_opens_and_closes_50(spillway):
    f = _Counting(spillway.app.state.conn_factory)
    spillway.app.state.conn_factory = f
    for _ in range(50):
        r = await spillway.client.get("/api/deployments")
        assert r.status_code == 200, r.text
    assert len(f.opened) == 50
    assert f.closed_count() == 50
    # 連線在事件迴圈執行緒開啟(handler 為 async;check_same_thread 不改)
    assert set(f.threads) == {threading.get_ident()}


async def test_models_endpoint_opens_and_closes_one(spillway):
    f = _Counting(spillway.app.state.conn_factory)
    spillway.app.state.conn_factory = f
    r = await spillway.client.get("/v1/models")
    assert r.status_code == 200, r.text
    assert len(f.opened) == 1 and f.closed_count() == 1


async def test_replaced_conn_factory_is_used_by_next_request(spillway):
    old = spillway.app.state.conn_factory
    r = await spillway.client.get("/api/deployments")
    assert r.status_code == 200
    f = _Counting(old)
    spillway.app.state.conn_factory = f
    r = await spillway.client.get("/api/deployments")
    assert r.status_code == 200
    assert len(f.opened) == 1


async def test_direct_handler_call_with_simple_namespace(spillway, fake_api):
    spillway.add_deployment(local_member("a", fake_api.base_url))
    f = _Counting(spillway.app.state.conn_factory)
    spillway.app.state.conn_factory = f
    out = await admin.test_deployment(SimpleNamespace(app=spillway.app), "a")
    assert out["deployment"] == "a" and out["ok"] is True, out
    assert len(f.opened) == 1 and f.closed_count() == 1
    assert len(spillway.ledger()) == 1

"""CHG-20261101-02 T2 L1:`store.db.ConnSource`、`store.db.use_conn`(決策 5)。

`use_conn` 一律不關閉連線:傳入 Connection 時直接交回;傳入零參數 callable 時呼叫一次並交回。
關閉責任只在開啟者(Router 的 `close_conn`、`api.deps.request_conn`、`RetentionScheduler`)。
"""
from __future__ import annotations

import contextlib
import sqlite3

import pytest

import spillway.estimate as estimate_mod
import spillway.quota as quota_mod
from spillway.clock import SeqRng
from spillway.domain import CallResult, ChatRequest, Deployment
from spillway.routing import resolver as resolver_mod
from spillway.routing.router import Router
from spillway.store import db as store_db
from spillway.store import repo
from spillway.store.db import ConnSource, use_conn
from spillway.store.repo import Route


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _OkAdapter:
    async def call(self, dep, req, effort):
        return CallResult(ok=True, text="hello", tokens_in=11, tokens_out=7, cost_usd=0.001,
                          status="ok", error=None)

    async def health(self, dep):
        return True


class _Registry:
    """鴨子型別的 AdapterRegistry:每個成員都可用、呼叫一律成功。"""

    def unavailable(self):
        return {}

    def module_for(self, dep):
        return object()

    def get(self, dep):
        return _OkAdapter()


def _save_dep(conn: sqlite3.Connection, dep_id: str, tag: str) -> None:
    repo.save_deployment(conn, Deployment(
        id=dep_id, model_name=dep_id, kind="api", provider="openai", model="gpt-4o-mini",
        params={}, credential_ref=None,
        capabilities={"ctx": 100000, "tools": True, "vision": True, "efforts": []},
        tags=(tag,)))


def _alive(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return False
    return True


def test_conn_source_alias_accepts_connection_and_callable():
    args = getattr(ConnSource, "__args__", ())
    assert sqlite3.Connection in args
    assert len(args) == 2


def test_use_conn_with_connection_yields_same_and_keeps_it_open(db):
    with use_conn(db) as conn:
        assert conn is db
        conn.execute("SELECT 1")
    assert _alive(db)


def test_use_conn_with_factory_calls_once_and_keeps_result_open(db):
    calls = []

    def factory():
        calls.append(1)
        return db

    with use_conn(factory) as conn:
        assert conn is db
        assert calls == [1]
    assert calls == [1]
    assert _alive(db)


def test_use_conn_does_not_close_on_exception(db):
    with pytest.raises(ValueError):
        with use_conn(lambda: db):
            raise ValueError("boom")
    assert _alive(db)


@pytest.mark.parametrize("bad", [42, "path/to.db", None, object()])
def test_use_conn_rejects_other_types(bad):
    with pytest.raises(TypeError):
        with use_conn(bad):
            pass


def test_private_conn_helpers_removed():
    # resolver、quota、estimate 不再各自實作 `_conn()` 與 `ConnSource`
    assert not hasattr(resolver_mod.Resolver, "_conn")
    assert not hasattr(quota_mod.QuotaService, "_conn")
    assert not hasattr(estimate_mod.Estimator, "_conn")
    for mod in (resolver_mod, quota_mod, estimate_mod):
        assert getattr(mod, "ConnSource", ConnSource) is ConnSource


@pytest.mark.anyio
async def test_router_contextvar_conn_stays_open_until_router_close(tmp_path, clock, monkeypatch):
    path = tmp_path / "r.db"
    setup = store_db.connect(path)
    _save_dep(setup, "a", "r")
    repo.save_route(setup, Route("r", {"tags": {"all": ["r"]}}))

    opened: list[sqlite3.Connection] = []

    def factory():
        c = store_db.connect(path)
        opened.append(c)
        return c

    used: dict[str, int] = {}
    real = store_db.use_conn

    def spy(name):
        @contextlib.contextmanager
        def _wrapped(source):
            with real(source) as conn:
                yield conn
            # 每次 use_conn 離開後,Router 的請求連線仍必須可用
            assert _alive(conn), f"{name} 的 use_conn 關閉了連線"
            used[name] = used.get(name, 0) + 1
        return _wrapped

    for name, mod in (("resolver", resolver_mod), ("quota", quota_mod),
                      ("estimate", estimate_mod)):
        monkeypatch.setattr(mod, "use_conn", spy(name))

    closed_at: list[bool] = []
    real_close = Router._close

    def close_spy(self, conn, token):
        closed_at.append(_alive(conn))          # _close 之前連線仍可用
        real_close(self, conn, token)

    monkeypatch.setattr(Router, "_close", close_spy)

    # 預設的 QuotaService、Estimator、LedgerWriter 都經 Router 的 contextvar 連線
    r = Router(factory, _Registry(), clock, SeqRng([0.0]), close_conn=True)
    req = ChatRequest(model="r", messages=[{"role": "user", "content": "hi"}])
    out = await r.route(req)
    assert out.ok and out.served_by == "a"
    assert len(opened) == 1                     # 一次請求只開一條連線
    assert closed_at == [True]
    assert not _alive(opened[0])                # 由 Router 的 _close 關閉
    assert used.get("resolver", 0) >= 1 and used.get("quota", 0) >= 1
    assert used.get("estimate", 0) >= 1
    assert len(repo.list_ledger(setup)) == 1
    setup.close()

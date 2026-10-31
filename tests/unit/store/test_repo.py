"""T2 L2:各表經 repo 寫入再讀回一致;load_deployment 產出 T4 的 Deployment;
replace_route_targets 單一交易;settings 缺鍵取常數預設值。"""
from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from spillway.adapters.base.types import Budget, Deployment
from spillway.store import repo
from spillway.store.db import transaction

T0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)


def _dep(dep_id: str = "d1", **kw) -> Deployment:
    base = dict(
        id=dep_id,
        model_name="claude-sonnet",
        kind="api",
        provider="anthropic",
        model="claude-sonnet-4",
        params={"api_base": "http://127.0.0.1:9", "timeout": 30, "signals": {"quota": "x"}},
        credential_ref="env:SPILLWAY_TEST_KEY",
        capabilities={"ctx": 200000, "tools": True, "vision": False, "efforts": ["low", "high"]},
        enabled=True,
        tags=("cheap", "fast"),
        budget=Budget(
            type="usd",
            limit_amount=20.0,
            window="monthly",
            reset_anchor=5,
            exhausted_until=T0 + timedelta(hours=3),
        ),
    )
    base.update(kw)
    return Deployment(**base)


def _row_count(db, table):
    return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------- deployments / tags / budgets ----------

def test_deployment_roundtrip_full(db):
    dep = _dep()
    repo.save_deployment(db, dep)
    got = repo.load_deployment(db, "d1")
    assert got == dep
    assert isinstance(got, Deployment)


def test_load_deployment_field_types_match_contract(db):
    repo.save_deployment(db, _dep())
    got = repo.load_deployment(db, "d1")
    names = {f.name for f in dataclasses.fields(Deployment)}
    assert names == {f.name for f in dataclasses.fields(got)}
    assert isinstance(got.params, dict)
    assert isinstance(got.capabilities, dict)
    assert isinstance(got.enabled, bool)
    assert isinstance(got.tags, tuple)
    assert isinstance(got.budget, Budget)
    assert got.budget.exhausted_until.tzinfo is not None
    assert got.budget.exhausted_until == T0 + timedelta(hours=3)


def test_deployment_roundtrip_minimal(db):
    dep = Deployment(id="loc", model_name="llama", kind="local", provider="hosted_vllm", model="m")
    repo.save_deployment(db, dep)
    got = repo.load_deployment(db, "loc")
    assert got == dep
    assert got.budget is None
    assert got.tags == ()
    assert got.credential_ref is None
    assert _row_count(db, "budgets") == 0


def test_deployment_stored_columns(db):
    repo.save_deployment(db, _dep(enabled=False))
    row = db.execute("SELECT * FROM deployments WHERE id='d1'").fetchone()
    assert row["enabled"] == 0
    assert row["params"].startswith("{")
    b = db.execute("SELECT * FROM budgets WHERE deployment_id='d1'").fetchone()
    assert b["exhausted_until"] == "2026-10-30T15:00:00Z"
    tags = [r[0] for r in db.execute("SELECT tag FROM deployment_tags ORDER BY tag")]
    assert tags == ["cheap", "fast"]


def test_load_missing_deployment_returns_none(db):
    assert repo.load_deployment(db, "nope") is None


def test_save_deployment_update_replaces_tags_budget_and_keeps_route_targets(db):
    repo.save_deployment(db, _dep())
    repo.save_route(db, repo.Route(name="r", target={"group": "g"}))
    repo.replace_route_targets(db, "r", [repo.RouteTarget("d1", slot=1, position=0)])

    updated = _dep(model="claude-opus", tags=("smart",), budget=None, enabled=False)
    repo.save_deployment(db, updated)

    assert repo.load_deployment(db, "d1") == updated
    assert _row_count(db, "budgets") == 0
    # 以 UPSERT 更新,不是 DELETE + INSERT,所以 route_targets 不被串聯刪除
    assert [t.deployment_id for t in repo.list_route_targets(db, "r")] == ["d1"]


def test_list_deployments(db):
    repo.save_deployment(db, _dep("b"))
    repo.save_deployment(db, _dep("a", enabled=False))
    assert [d.id for d in repo.list_deployments(db)] == ["a", "b"]
    assert [d.id for d in repo.list_deployments(db, enabled_only=True)] == ["b"]


def test_delete_deployment(db):
    repo.save_deployment(db, _dep())
    assert repo.delete_deployment(db, "d1") is True
    assert repo.delete_deployment(db, "d1") is False
    assert repo.load_deployment(db, "d1") is None
    assert _row_count(db, "deployment_tags") == 0
    assert _row_count(db, "budgets") == 0


def test_tags_roundtrip(db):
    repo.save_deployment(db, _dep(tags=()))
    repo.set_tags(db, "d1", ["z", "a", "a"])
    assert repo.get_tags(db, "d1") == ("a", "z")
    repo.set_tags(db, "d1", [])
    assert repo.get_tags(db, "d1") == ()


def test_budget_roundtrip_and_cooldown(db):
    repo.save_deployment(db, _dep(budget=None))
    assert repo.get_budget(db, "d1") is None
    b = Budget(type="credit_pool", limit_amount=None, window="none")
    repo.save_budget(db, "d1", b)
    assert repo.get_budget(db, "d1") == b

    until = T0 + timedelta(minutes=30)
    assert repo.set_exhausted_until(db, "d1", until) is True
    assert repo.get_budget(db, "d1").exhausted_until == until
    assert repo.set_exhausted_until(db, "d1", None) is True
    assert repo.get_budget(db, "d1").exhausted_until is None
    assert repo.set_exhausted_until(db, "ghost", until) is False

    repo.save_budget(db, "d1", None)
    assert repo.get_budget(db, "d1") is None


# ---------- groups ----------

def test_group_roundtrip(db):
    g = repo.Group(name="g1", selector={"all": ["a"], "any": ["b", "c"], "none": []})
    repo.save_group(db, g)
    assert repo.get_group(db, "g1") == g
    g2 = repo.Group(name="g1", selector={"all": [], "any": [], "none": ["x"]})
    repo.save_group(db, g2)
    assert repo.get_group(db, "g1") == g2
    repo.save_group(db, repo.Group(name="a0", selector={"all": [], "any": [], "none": []}))
    assert [g.name for g in repo.list_groups(db)] == ["a0", "g1"]
    assert repo.delete_group(db, "g1") is True
    assert repo.get_group(db, "g1") is None
    assert repo.delete_group(db, "g1") is False


# ---------- routes / route_targets ----------

def test_route_roundtrip(db):
    r = repo.Route(
        name="coding",
        target={"tags": {"all": ["code"], "any": [], "none": ["slow"]}},
        mode="task",
        tags=("x", "y"),
        record_context=False,
        budget_precheck=True,
    )
    repo.save_route(db, r)
    assert repo.get_route(db, "coding") == r
    d = repo.Route(name="chat", target={"group": "g"})
    repo.save_route(db, d)
    got = repo.get_route(db, "chat")
    assert got == d
    assert got.record_context is None and got.budget_precheck is False and got.mode == "chat"
    raw = db.execute("SELECT * FROM routes WHERE name='coding'").fetchone()
    assert raw["record_context"] == 0 and raw["budget_precheck"] == 1
    assert [x.name for x in repo.list_routes(db)] == ["chat", "coding"]


def test_route_update_keeps_targets_and_delete_cascades(db):
    repo.save_deployment(db, _dep())
    repo.save_route(db, repo.Route(name="r", target={"group": "g"}))
    repo.replace_route_targets(db, "r", [repo.RouteTarget("d1", slot=1, position=0)])
    repo.save_route(db, repo.Route(name="r", target={"group": "h"}, record_context=True))
    assert repo.get_route(db, "r").record_context is True
    assert len(repo.list_route_targets(db, "r")) == 1
    assert repo.delete_route(db, "r") is True
    assert repo.get_route(db, "r") is None
    assert _row_count(db, "route_targets") == 0
    assert repo.delete_route(db, "r") is False


def _seed_targets(db):
    for i in ("a", "b", "c"):
        repo.save_deployment(db, _dep(i))
    repo.save_route(db, repo.Route(name="r", target={"group": "g"}))
    old = [
        repo.RouteTarget("a", slot=1, position=0, effort="high", weight="high"),
        repo.RouteTarget("b", slot=1, position=1),
        repo.RouteTarget("c", slot=2, position=0, effort="none", weight="low"),
    ]
    repo.replace_route_targets(db, "r", old)
    return old


def test_route_targets_roundtrip_ordered(db):
    old = _seed_targets(db)
    assert repo.list_route_targets(db, "r") == old
    new = [
        repo.RouteTarget("c", slot=1, position=0),
        repo.RouteTarget("a", slot=2, position=1),
        repo.RouteTarget("b", slot=2, position=0, effort="max"),
    ]
    repo.replace_route_targets(db, "r", new)
    assert repo.list_route_targets(db, "r") == [new[0], new[2], new[1]]
    repo.replace_route_targets(db, "r", [])
    assert repo.list_route_targets(db, "r") == []


def test_replace_route_targets_rolls_back_on_exception_midway(db):
    old = _seed_targets(db)

    def rows():
        yield repo.RouteTarget("a", slot=9, position=0)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        repo.replace_route_targets(db, "r", rows())
    assert repo.list_route_targets(db, "r") == old
    assert not db.in_transaction


def test_replace_route_targets_rolls_back_on_constraint_violation(db):
    old = _seed_targets(db)
    bad = [
        repo.RouteTarget("a", slot=1, position=0),
        repo.RouteTarget("b", slot=1, position=1, weight="huge"),
    ]
    with pytest.raises(sqlite3.IntegrityError):
        repo.replace_route_targets(db, "r", bad)
    assert repo.list_route_targets(db, "r") == old

    with pytest.raises(sqlite3.IntegrityError):  # 外鍵:不存在的成員
        repo.replace_route_targets(db, "r", [repo.RouteTarget("ghost", slot=1, position=0)])
    assert repo.list_route_targets(db, "r") == old


def test_replace_route_targets_inside_outer_transaction_is_savepoint(db):
    old = _seed_targets(db)
    with pytest.raises(RuntimeError):
        with transaction(db):
            repo.replace_route_targets(db, "r", [repo.RouteTarget("a", slot=5, position=0)])
            raise RuntimeError("outer fails")
    assert repo.list_route_targets(db, "r") == old


# ---------- usage_ledger / usage_payloads ----------

def _ledger(**kw) -> repo.LedgerRow:
    base = dict(
        ts=T0,
        route_name="coding",
        deployment_id="d1",
        effort="high",
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.0125,
        status="ok",
        slot=1,
        fallback_depth=0,
        latency_ms=321,
        est_tokens_in=110,
        est_tokens_out_p50=40,
        est_tokens_out_p90=90,
        est_cost_usd_p90=0.02,
        error=None,
    )
    base.update(kw)
    return repo.LedgerRow(**base)


def test_ledger_roundtrip(db):
    row = _ledger()
    new_id = repo.insert_ledger(db, row)
    assert isinstance(new_id, int)
    got = repo.get_ledger(db, new_id)
    assert got == dataclasses.replace(row, id=new_id)
    assert db.execute("SELECT ts FROM usage_ledger").fetchone()[0] == "2026-10-30T12:00:00Z"


def test_ledger_roundtrip_mostly_null(db):
    row = repo.LedgerRow(ts=T0, status="skipped_precheck")
    new_id = repo.insert_ledger(db, row)
    assert repo.get_ledger(db, new_id) == dataclasses.replace(row, id=new_id)
    assert repo.get_ledger(db, 9999) is None


def test_list_ledger_filters_newest_first(db):
    ids = []
    for i, (dep, status) in enumerate(
        [("d1", "ok"), ("d2", "ok"), ("d1", "error"), ("d1", "ok"), ("d1", "ok")]
    ):
        ids.append(repo.insert_ledger(db, _ledger(ts=T0 + timedelta(minutes=i), deployment_id=dep, status=status)))
    got = repo.list_ledger(db, deployment_id="d1", status="ok")
    assert [r.id for r in got] == [ids[4], ids[3], ids[0]]
    got = repo.list_ledger(db, deployment_id="d1", status="ok", limit=2)
    assert [r.id for r in got] == [ids[4], ids[3]]
    got = repo.list_ledger(db, since=T0 + timedelta(minutes=2))
    assert [r.id for r in got] == [ids[4], ids[3], ids[2]]
    got = repo.list_ledger(db, until=T0 + timedelta(minutes=2))
    assert [r.id for r in got] == [ids[1], ids[0]]
    got = repo.list_ledger(db, limit=2, offset=1)
    assert [r.id for r in got] == [ids[3], ids[2]]
    assert repo.list_ledger(db, route_name="other") == []


def test_payload_roundtrip_and_cascade(db):
    lid = repo.insert_ledger(db, _ledger())
    p = repo.Payload(
        ledger_id=lid,
        ts=T0,
        request_json='[{"role": "user", "content": "hi"}]',
        response_text="hello",
        bytes=42,
        truncated=True,
        redacted=False,
    )
    repo.insert_payload(db, p)
    assert repo.get_payload(db, lid) == p
    raw = db.execute("SELECT truncated, redacted, ts FROM usage_payloads").fetchone()
    assert tuple(raw) == (1, 0, "2026-10-30T12:00:00Z")
    assert repo.get_payload(db, 9999) is None
    db.execute("DELETE FROM usage_ledger WHERE id = ?", (lid,))
    assert repo.get_payload(db, lid) is None


def test_payload_with_null_response(db):
    lid = repo.insert_ledger(db, _ledger(status="error"))
    p = repo.Payload(ledger_id=lid, ts=T0, request_json="[]", response_text=None, bytes=2)
    repo.insert_payload(db, p)
    assert repo.get_payload(db, lid) == p


def test_delete_payloads_before(db):
    lids = []
    for days in (10, 8, 1):
        lid = repo.insert_ledger(db, _ledger(ts=T0 - timedelta(days=days)))
        repo.insert_payload(
            db, repo.Payload(ledger_id=lid, ts=T0 - timedelta(days=days), request_json="[]", bytes=2)
        )
        lids.append(lid)
    n = repo.delete_payloads_before(db, T0 - timedelta(days=7))
    assert n == 2
    assert [repo.get_payload(db, x) is None for x in lids] == [True, True, False]
    assert _row_count(db, "usage_ledger") == 3  # 帳本列保留


# ---------- settings ----------

def test_settings_defaults_without_writing_rows(db):
    assert repo.get_setting(db, "record_context_default") == "1"
    assert repo.get_setting(db, "context_retention_days") == "7"
    assert repo.get_setting(db, "context_max_bytes") == "262144"
    assert repo.get_setting(db, "unknown_key") is None
    cs = repo.get_context_settings(db)
    assert cs == repo.ContextSettings(record_default=True, retention_days=7, max_bytes=262144)
    assert _row_count(db, "settings") == 0


def test_settings_roundtrip(db):
    repo.set_setting(db, "context_retention_days", "0")
    repo.set_setting(db, "record_context_default", "0")
    repo.set_setting(db, "context_max_bytes", 1024)
    repo.set_setting(db, "custom", "abc")
    assert repo.get_setting(db, "context_retention_days") == "0"
    assert repo.get_setting(db, "custom") == "abc"
    assert repo.get_context_settings(db) == repo.ContextSettings(
        record_default=False, retention_days=0, max_bytes=1024
    )
    repo.set_setting(db, "custom", "def")
    assert repo.get_setting(db, "custom") == "def"
    assert _row_count(db, "settings") == 4


def test_settings_defaults_constant():
    assert repo.SETTING_DEFAULTS == {
        "record_context_default": "1",
        "context_retention_days": "7",
        "context_max_bytes": "262144",
    }


# ---------- 交易 ----------

def test_writes_visible_to_another_connection(db, tmp_path):
    from spillway.store.db import connect

    repo.save_deployment(db, _dep())
    path = db.execute("PRAGMA database_list").fetchone()[2]
    other = connect(path)
    try:
        assert repo.load_deployment(other, "d1") == _dep()
    finally:
        other.close()


def test_save_deployment_atomic_on_bad_budget(db):
    bad = _dep(budget=Budget(type="bogus", limit_amount=1.0))
    with pytest.raises(sqlite3.IntegrityError):
        repo.save_deployment(db, bad)
    assert repo.load_deployment(db, "d1") is None
    assert _row_count(db, "deployment_tags") == 0


# ---------- 審查回修 M3、S-T2-1 ----------

@pytest.mark.parametrize("value, expected", [(True, True), (False, False)])
def test_set_setting_bool_roundtrip(db, value, expected):
    """M3:bool 寫入存成 "1"/"0",get_context_settings 讀回同值。"""
    repo.set_setting(db, "record_context_default", value)
    assert repo.get_setting(db, "record_context_default") == ("1" if expected else "0")
    assert repo.get_context_settings(db).record_default is expected


def test_set_setting_bool_overwrite_flips_both_ways(db):
    repo.set_setting(db, "record_context_default", False)
    assert repo.get_context_settings(db).record_default is False
    repo.set_setting(db, "record_context_default", True)
    assert repo.get_context_settings(db).record_default is True


def test_savepoint_keeps_original_exception_when_sqlite_already_rolled_back(db):
    """S-T2-1:外層交易已被回滾(SAVEPOINT 隨之消失)時,原例外照原樣往外丟。"""
    with pytest.raises(ValueError, match="original"):
        with transaction(db):
            with transaction(db):
                db.execute("ROLLBACK")  # 模擬 SQLite 自動回滾整個交易
                raise ValueError("original")
    assert not db.in_transaction


def test_mark_exhausted_until_inserts_none_row(db):
    repo.save_deployment(db, _dep(budget=None))
    repo.mark_exhausted_until(db, "d1", T0)
    b = repo.get_budget(db, "d1")
    assert (b.type, b.limit_amount, b.window, b.reset_anchor) == ("none", None, "none", None)
    assert b.exhausted_until == T0


def test_mark_exhausted_until_conflict_only_updates_cooldown(db):
    repo.save_deployment(db, _dep(budget=None))
    orig = Budget(type="usd", limit_amount=20.0, window="monthly", reset_anchor=5)
    repo.save_budget(db, "d1", orig)
    repo.mark_exhausted_until(db, "d1", T0)
    assert repo.get_budget(db, "d1") == dataclasses.replace(orig, exhausted_until=T0)

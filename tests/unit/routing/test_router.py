"""T12 L1/L2:Router(spec 4.5、5.4.1、5.4.5、6.1、6.2;決策 18、19、20、29)。

L1 以 `ScriptedRegistry` 控制每個成員的 CallResult;L2 以 T6 內建 adapter 經 LiteLLM 打 T5 假端點。
帳本、額度一律用真的 `LedgerWriter`、`QuotaService` 與 tmp_path 資料庫。
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

from spillway import credentials
from spillway.adapters.base import AdapterContext, AdapterRegistry
from spillway.clock import SeqRng
from spillway.domain import Budget, ChatRequest, Deployment
from spillway.routing import RouteNotFound
from spillway.routing.router import (
    InvalidEffort,
    Plan,
    RouteOutcome,
    Router,
    normalize_effort,
)
from spillway.store import repo
from spillway.store.repo import Route, RouteTarget

from .helpers import make_dep
from .router_helpers import EST, FixedEstimator, ScriptedRegistry, fail, ok

pytestmark = pytest.mark.anyio

MSGS = [{"role": "user", "content": "hi"}]


def req(model: str = "r", **kw) -> ChatRequest:
    return ChatRequest(model=model, messages=kw.pop("messages", MSGS), **kw)


def make_route(db, name="r", *, targets=(), precheck=False, record_context=None, mode="chat"):
    repo.save_route(db, Route(name, {"tags": {"all": [name]}}, mode=mode,
                              budget_precheck=precheck, record_context=record_context))
    if targets:
        repo.replace_route_targets(db, name, [RouteTarget(*t) if isinstance(t, tuple) else t
                                              for t in targets])


def router(db, clock, reg, *, rng=None, estimator=None, **kw) -> Router:
    return Router(lambda: db, reg, clock, rng or SeqRng([0.0]),
                  estimator=estimator or FixedEstimator(), **kw)


def ledger(db):
    """帳本列,舊到新。"""
    return list(reversed(repo.list_ledger(db)))


def payload_count(db) -> int:
    return db.execute("SELECT COUNT(*) FROM usage_payloads").fetchone()[0]


def cooling_budget(clock):
    return Budget("usd", 10.0, "daily", exhausted_until=clock.now() + timedelta(hours=1))


# ================================================================== normalize_effort(決策 29)


@pytest.mark.parametrize(("raw", "want"), [
    (None, None), ("high", "high"), ("HIGH", "high"), ("Max", "max"), ("minimal", "none"),
    ("MINIMAL", "none"), ("none", "none"), (" low ", "low"), ("", None),
])
def test_normalize_effort(raw, want):
    assert normalize_effort(raw) == want


@pytest.mark.parametrize("raw", ["xhigh", "medium-high", 3, "ultra"])
def test_normalize_effort_rejects(raw):
    with pytest.raises(InvalidEffort) as ei:
        normalize_effort(raw)
    assert isinstance(ei.value, ValueError)


async def test_route_rejects_invalid_effort_before_any_call(db, clock):
    make_dep(db, "a", tags=("r",))
    make_route(db)
    reg = ScriptedRegistry()
    with pytest.raises(InvalidEffort):
        await router(db, clock, reg).route(req(reasoning_effort="turbo"))
    assert reg.calls == [] and ledger(db) == []


async def test_route_passes_normalized_effort(db, clock):
    make_dep(db, "a", tags=("r",), capabilities={"efforts": ["none", "low", "high"]})
    make_route(db)
    reg = ScriptedRegistry()
    await router(db, clock, reg).route(req(reasoning_effort="MINIMAL"))
    assert reg.calls[0][1] == "none"
    assert ledger(db)[0].effort == "none"


# ================================================================== 解析


async def test_unknown_model_raises_route_not_found(db, clock):
    with pytest.raises(RouteNotFound):
        await router(db, clock, ScriptedRegistry()).route(req("nope"))


# ================================================================== L1:fallback 與帳本


async def test_slot1_all_cooling_slot2_serves(db, clock):
    make_dep(db, "a", tags=("r",), budget=cooling_budget(clock))
    make_dep(db, "b", tags=("r",), budget=cooling_budget(clock))
    make_dep(db, "c", tags=("r",))
    make_route(db, targets=[("a", 1, 0), ("b", 1, 1), ("c", 2, 0)])
    reg = ScriptedRegistry()
    out = await router(db, clock, reg).route(req())
    assert out.ok and out.served_by == "c" and out.slot == 2
    assert reg.called_ids() == ["c"]
    rows = ledger(db)
    assert [(r.deployment_id, r.status, r.slot, r.fallback_depth) for r in rows] == [
        ("c", "ok", 2, 0)]          # 冷卻成員沒有被呼叫,不算 fallback 深度
    assert dict(out.excluded) == {"a": "cooling", "b": "cooling"}


async def test_fallback_depth_counts_prior_attempts(db, clock):
    for d in ("a", "b", "c"):
        make_dep(db, d, tags=("r",))
    make_route(db, targets=[("a", 1, 0), ("b", 2, 0), ("c", 3, 0)])
    reg = ScriptedRegistry()
    reg.script = {"a": fail(), "b": fail("rate_limited")}
    out = await router(db, clock, reg).route(req())
    assert out.served_by == "c" and out.slot == 3
    assert [(r.deployment_id, r.slot, r.fallback_depth) for r in ledger(db)] == [
        ("a", 1, 0), ("b", 2, 1), ("c", 3, 2)]


async def test_429_cools_member_60s_and_next_serves(db, clock):
    make_dep(db, "a", tags=("r",), budget=Budget("usd", 10.0, "monthly", reset_anchor=1))
    make_dep(db, "b", tags=("r",))
    make_route(db, targets=[("a", 1, 0), ("b", 1, 1)])
    reg = ScriptedRegistry()
    reg.script = {"a": fail("rate_limited", "429")}
    out = await router(db, clock, reg, rng=SeqRng([0.0])).route(req())
    assert reg.called_ids() == ["a", "b"]
    assert out.served_by == "b"
    assert repo.get_budget(db, "a").exhausted_until == clock.now() + timedelta(seconds=60)
    assert [r.status for r in ledger(db)] == ["rate_limited", "ok"]


async def test_429_on_member_without_budget_still_cools(db, clock):
    make_dep(db, "a", tags=("r",))
    make_dep(db, "b", tags=("r",))
    make_route(db, targets=[("a", 1, 0), ("b", 2, 0)])
    reg = ScriptedRegistry()
    reg.script = {"a": fail("rate_limited")}
    await router(db, clock, reg).route(req())
    assert repo.get_budget(db, "a").exhausted_until == clock.now() + timedelta(seconds=60)


async def test_402_cools_until_next_reset(db, clock):
    make_dep(db, "a", tags=("r",), budget=Budget("usd", 10.0, "monthly", reset_anchor=1))
    make_dep(db, "b", tags=("r",))
    make_route(db, targets=[("a", 1, 0), ("b", 2, 0)])
    reg = ScriptedRegistry()
    reg.script = {"a": fail("quota_exhausted", "402 payment required")}
    out = await router(db, clock, reg).route(req())
    assert out.served_by == "b"
    assert repo.get_budget(db, "a").exhausted_until == datetime(2026, 11, 1, tzinfo=timezone.utc)


async def test_error_does_not_cool(db, clock):
    make_dep(db, "a", tags=("r",), budget=Budget("usd", 10.0, "daily"))
    make_dep(db, "b", tags=("r",))
    make_route(db, targets=[("a", 1, 0), ("b", 2, 0)])
    reg = ScriptedRegistry()
    reg.script = {"a": fail("error")}
    await router(db, clock, reg).route(req())
    assert repo.get_budget(db, "a").exhausted_until is None


async def test_local_member_rate_limited_not_marked(db, clock):
    make_dep(db, "loc", kind="local", tags=("r",))
    make_dep(db, "b", tags=("r",))
    make_route(db, targets=[("loc", 1, 0), ("b", 2, 0)])
    reg = ScriptedRegistry()
    reg.script = {"loc": fail("rate_limited")}
    out = await router(db, clock, reg).route(req())
    assert out.served_by == "b"
    assert repo.get_budget(db, "loc") is None


async def test_all_fail_attempts_in_order(db, clock):
    for d in ("a", "b", "c", "d"):
        make_dep(db, d, tags=("r",))
    make_route(db, targets=[("a", 1, 0), ("b", 1, 1), ("c", 2, 0), ("d", 3, 0)])
    reg = ScriptedRegistry()
    reg.script = {"a": fail("error", "e-a"), "b": fail("rate_limited", "e-b"),
                  "c": fail("quota_exhausted", "e-c"), "d": fail("error", "e-d")}
    # SeqRng 0.99:slot 1 內先抽中權重累計的最後一位(b),再 a
    out = await router(db, clock, reg, rng=SeqRng([0.99])).route(req())
    assert not out.ok and out.result is None and out.served_by is None and out.slot is None
    assert [(a.deployment, a.status, a.slot, a.error) for a in out.attempts] == [
        ("b", "rate_limited", 1, "e-b"), ("a", "error", 1, "e-a"),
        ("c", "quota_exhausted", 2, "e-c"), ("d", "error", 3, "e-d")]
    assert all(a.latency_ms is not None and a.latency_ms >= 0 for a in out.attempts)
    assert [r.deployment_id for r in ledger(db)] == ["b", "a", "c", "d"]


async def test_no_candidate_when_everything_filtered(db, clock):
    make_dep(db, "a", tags=("r",), enabled=False)
    make_route(db)
    out = await router(db, clock, ScriptedRegistry()).route(req())
    assert not out.ok and out.attempts == ()
    assert out.excluded == (("a", "disabled"),)
    assert ledger(db) == []


@pytest.mark.parametrize("status", ["ok", "error", "quota_exhausted", "rate_limited"])
async def test_call_status_maps_to_ledger_status(db, clock, status):
    make_dep(db, "a", tags=("r",))
    make_route(db)
    reg = ScriptedRegistry()
    reg.script = {"a": ok() if status == "ok" else fail(status)}
    out = await router(db, clock, reg).route(req())
    (row,) = ledger(db)
    assert row.status == status
    assert out.ok is (status == "ok")
    assert out.attempts[0].status == status
    assert (row.error is None) is (status == "ok")


async def test_unknown_call_status_recorded_as_error(db, clock):
    make_dep(db, "a", tags=("r",))
    make_dep(db, "b", tags=("r",))
    make_route(db, targets=[("a", 1, 0), ("b", 2, 0)])
    reg = ScriptedRegistry()
    reg.script = {"a": dataclasses.replace(fail(), status="weird")}
    out = await router(db, clock, reg).route(req())
    assert out.served_by == "b"
    assert [r.status for r in ledger(db)] == ["error", "ok"]


async def test_adapter_exception_is_error_and_falls_back(db, clock):
    make_dep(db, "a", tags=("r",))
    make_dep(db, "b", tags=("r",))
    make_route(db, targets=[("a", 1, 0), ("b", 2, 0)])
    reg = ScriptedRegistry()
    reg.script = {"a": RuntimeError("kaboom")}
    out = await router(db, clock, reg).route(req())
    assert out.served_by == "b"
    assert out.attempts[0].status == "error" and "kaboom" in out.attempts[0].error
    assert ledger(db)[0].status == "error"


async def test_success_ledger_row_fields(db, clock):
    make_dep(db, "a", tags=("r",))
    make_route(db, record_context=True)
    reg = ScriptedRegistry()
    reg.script = {"a": ok("answer", tokens_in=3, tokens_out=4, cost_usd=0.5)}
    out = await router(db, clock, reg).route(req())
    assert out.result.text == "answer" and out.estimate == EST
    (row,) = ledger(db)
    assert (row.route_name, row.deployment_id, row.slot, row.fallback_depth, row.ts) == (
        "r", "a", 1, 0, clock.now())
    assert (row.tokens_in, row.tokens_out, row.cost_usd) == (3, 4, 0.5)
    assert (row.est_tokens_in, row.est_tokens_out_p50, row.est_tokens_out_p90,
            row.est_cost_usd_p90) == (10, 500, 2000, 0.01)
    assert row.latency_ms is not None and row.latency_ms >= 0
    p = repo.get_payload(db, row.id)
    assert json.loads(p.request_json) == MSGS
    assert p.response_text == "answer"


async def test_payload_for_failed_attempt_and_tool_calls(db, clock):
    make_dep(db, "a", tags=("r",))
    make_dep(db, "b", tags=("r",))
    make_route(db, targets=[("a", 1, 0), ("b", 2, 0)])
    calls = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
    reg = ScriptedRegistry()
    reg.script = {"a": fail(), "b": ok("", tool_calls=calls, finish_reason="tool_calls")}
    await router(db, clock, reg).route(req())
    ra, rb = ledger(db)
    pa, pb = repo.get_payload(db, ra.id), repo.get_payload(db, rb.id)
    assert json.loads(pa.request_json) == MSGS and pa.response_text is None
    assert json.loads(pb.response_text) == {"content": "", "tool_calls": calls,
                                            "finish_reason": "tool_calls"}


async def test_route_record_context_off_writes_no_payload(db, clock):
    make_dep(db, "a", tags=("r",))
    make_route(db, record_context=False)
    await router(db, clock, ScriptedRegistry()).route(req())
    assert len(ledger(db)) == 1 and payload_count(db) == 0


async def test_implicit_route_ledger_route_name_is_model_name(db, clock):
    make_dep(db, "a1", model_name="gpt")
    out = await router(db, clock, ScriptedRegistry()).route(req("gpt"))
    assert out.served_by == "a1" and out.slot == 1
    assert ledger(db)[0].route_name == "gpt"


# ------------------------------------------------------------------ skipped_precheck(4.2.5)


async def test_skipped_precheck_writes_row_and_next_serves(db, clock):
    make_dep(db, "poor", tags=("r",), budget=Budget("usd", 0.005, "daily"))
    make_dep(db, "rich", tags=("r",))
    make_route(db, precheck=True, targets=[("poor", 1, 0), ("rich", 2, 0)])
    reg = ScriptedRegistry()
    out = await router(db, clock, reg).route(req())
    assert reg.called_ids() == ["rich"]
    assert out.served_by == "rich"
    assert [(a.deployment, a.status, a.slot) for a in out.attempts] == [
        ("poor", "skipped_precheck", 1), ("rich", "ok", 2)]
    skipped, served = ledger(db)
    assert (skipped.status, skipped.slot, skipped.effort, skipped.latency_ms) == (
        "skipped_precheck", 1, None, None)
    assert skipped.est_cost_usd_p90 == 0.01 and skipped.est_tokens_in == 10
    assert repo.get_payload(db, skipped.id) is None      # 沒有送出內容,不寫 payload
    assert repo.get_budget(db, "poor").exhausted_until is None   # 不設冷卻
    assert served.fallback_depth == 0


# ------------------------------------------------------------------ local/ 直呼(4.1.1、決策 18)


async def test_local_direct_calls_once_without_routing(db, clock):
    make_dep(db, "loc", kind="local", model_name="llama")
    make_dep(db, "other", kind="local", model_name="llama2")
    reg = ScriptedRegistry()
    reg.healthy["loc"] = False                       # 直呼不看健康
    out = await router(db, clock, reg).route(req("local/loc"))
    assert out.ok and out.served_by == "loc" and out.slot is None
    assert reg.health_calls == []
    (row,) = ledger(db)
    assert (row.slot, row.route_name, row.fallback_depth) == (None, None, 0)


async def test_local_direct_failure_no_fallback(db, clock):
    make_dep(db, "loc", kind="local", model_name="llama")
    make_dep(db, "loc2", kind="local", model_name="llama")
    reg = ScriptedRegistry()
    reg.script = {"loc": fail("error", "down")}
    out = await router(db, clock, reg).route(req("local/loc"))
    assert not out.ok
    assert [(a.deployment, a.status, a.slot) for a in out.attempts] == [("loc", "error", None)]
    assert reg.called_ids() == ["loc"]
    assert len(ledger(db)) == 1


async def test_local_direct_disabled_is_no_candidate(db, clock):
    make_dep(db, "loc", kind="local", enabled=False)
    reg = ScriptedRegistry()
    out = await router(db, clock, reg).route(req("local/loc"))
    assert not out.ok
    assert [(a.deployment, a.status) for a in out.attempts] == [("loc", "disabled")]
    assert reg.calls == [] and ledger(db) == []


async def test_local_direct_adapter_unavailable(db, clock):
    make_dep(db, "loc", kind="local", provider="brokenco")
    reg = ScriptedRegistry(unavailable_providers={"brokenco": "載入失敗"})
    out = await router(db, clock, reg).route(req("local/loc"))
    assert [(a.deployment, a.status) for a in out.attempts] == [("loc", "adapter_unavailable")]
    assert ledger(db) == []


# ------------------------------------------------------------------ effort(6.1、6.2)


async def test_route_target_effort_overrides_request_and_is_downgraded(db, clock):
    make_dep(db, "a", tags=("r",), capabilities={"efforts": ["low", "medium"]})
    make_route(db, targets=[RouteTarget("a", 1, 0, effort="max")])
    reg = ScriptedRegistry()
    await router(db, clock, reg).route(req(reasoning_effort="low"))
    assert reg.calls[0][1] == "medium"
    assert ledger(db)[0].effort == "medium"


async def test_request_effort_used_when_route_target_empty(db, clock):
    make_dep(db, "a", tags=("r",), capabilities={"efforts": ["low", "medium", "high"]})
    make_route(db)
    reg = ScriptedRegistry()
    await router(db, clock, reg).route(req(reasoning_effort="max"))
    assert reg.calls[0][1] == "high" and ledger(db)[0].effort == "high"


# ------------------------------------------------------------------ 秘密(安全影響 (d))


def _dep_with_ref(db, dep_id, ref, tags=("r",)):
    d = Deployment(id=dep_id, model_name=dep_id, kind="api", provider="openai", model="m",
                   credential_ref=ref, capabilities={"efforts": ["low"]}, tags=tags)
    repo.save_deployment(db, d)


async def test_echoed_secret_masked_in_ledger_and_attempts(db, clock, monkeypatch):
    secret = "fake-key-ABCD-1234-notsk"          # 非 sk- 格式:只能靠 extra_secrets 遮罩
    monkeypatch.setenv("SPILLWAY_TEST_KEY", secret)
    _dep_with_ref(db, "a", "env:SPILLWAY_TEST_KEY")
    make_route(db)
    reg = ScriptedRegistry()
    reg.script = {"a": fail("error", f"invalid api key: {secret}")}
    out = await router(db, clock, reg).route(req(messages=[{"role": "user", "content": secret}]))
    assert secret not in (out.attempts[0].error or "")
    assert "[REDACTED]" in out.attempts[0].error
    (row,) = ledger(db)
    assert secret not in row.error
    p = repo.get_payload(db, row.id)
    assert secret not in p.request_json and p.redacted


async def test_unresolvable_credential_still_calls_adapter(db, clock, monkeypatch):
    monkeypatch.delenv("SPILLWAY_TEST_KEY", raising=False)
    _dep_with_ref(db, "a", "env:SPILLWAY_TEST_KEY")
    make_route(db)
    reg = ScriptedRegistry()
    reg.script = {"a": fail("error", "credential_ref 無法解析")}
    out = await router(db, clock, reg).route(req())
    assert reg.called_ids() == ["a"] and out.attempts[0].status == "error"


async def test_extra_secrets_resolved_per_attempt(db, clock, monkeypatch):
    seen = []
    real = credentials.resolve
    monkeypatch.setattr(credentials, "resolve", lambda ref: seen.append(ref) or real(ref))
    monkeypatch.setenv("SPILLWAY_TEST_KEY", "sk-test-0000")
    monkeypatch.setenv("SPILLWAY_TEST_KEY2", "sk-test-0001")
    _dep_with_ref(db, "a", "env:SPILLWAY_TEST_KEY")
    _dep_with_ref(db, "b", "env:SPILLWAY_TEST_KEY2")
    make_route(db, targets=[("a", 1, 0), ("b", 2, 0)])
    reg = ScriptedRegistry()
    reg.script = {"a": fail()}
    await router(db, clock, reg).route(req())
    assert seen == ["env:SPILLWAY_TEST_KEY", "env:SPILLWAY_TEST_KEY2"]


# ------------------------------------------------------------------ 連線注入(design D2)


async def test_conn_factory_called_once_per_request(db, clock):
    make_dep(db, "a", tags=("r",))
    make_dep(db, "b", tags=("r",))
    make_route(db, targets=[("a", 1, 0), ("b", 2, 0)])
    n = []

    def factory():
        n.append(1)
        return db

    reg = ScriptedRegistry()
    reg.script = {"a": fail()}
    r = Router(factory, reg, clock, SeqRng([0.0]))      # 預設 QuotaService、Estimator、LedgerWriter
    out = await r.route(req())
    assert out.served_by == "b" and len(n) == 1
    await r.dry_run("r", req())
    assert len(n) == 2


async def test_close_conn_closes_factory_connection(tmp_path, clock):
    from spillway.store.db import connect
    path = tmp_path / "c.db"
    setup = connect(path)
    make_dep(setup, "a", tags=("r",))
    make_route(setup)
    opened = []

    def factory():
        c = connect(path)
        opened.append(c)
        return c

    r = Router(factory, ScriptedRegistry(), clock, SeqRng([0.0]), estimator=FixedEstimator(),
               close_conn=True)
    assert (await r.route(req())).ok
    with pytest.raises(Exception):
        opened[0].execute("SELECT 1")
    assert len(ledger(setup)) == 1
    setup.close()


# ================================================================== dry_run


async def test_dry_run_plan_no_side_effects(db, clock):
    make_dep(db, "a", tags=("r",), capabilities={"efforts": ["low", "medium"]})
    make_dep(db, "b", tags=("r",))
    make_dep(db, "new", tags=("r",))                                  # 不在 route_targets → unsorted
    make_dep(db, "hot", tags=("r",), budget=cooling_budget(clock))
    make_dep(db, "poor", tags=("r",), budget=Budget("usd", 0.005, "daily"))
    make_dep(db, "loc", kind="local", tags=("r",))
    make_route(db, precheck=True, targets=[
        RouteTarget("a", 1, 0, effort="high", weight="high"), ("b", 1, 1), ("hot", 1, 2),
        ("poor", 2, 0), ("loc", 3, 0)])
    reg = ScriptedRegistry()
    before = len(ledger(db))
    plan = await router(db, clock, reg, rng=SeqRng([0.0])).dry_run("r", req())
    assert isinstance(plan, Plan) and plan.route_name == "r"
    assert reg.calls == [] and reg.health_calls == []                  # 不呼叫、不探測
    assert len(ledger(db)) == before == 0
    assert repo.get_budget(db, "poor").exhausted_until is None
    assert {(e.deployment, e.reason) for e in plan.excluded} == {
        ("hot", "cooling"), ("poor", "skipped_precheck")}
    assert next(e for e in plan.excluded if e.deployment == "poor").estimate == EST
    layout = [(s.number, [e.deployment for e in s.entries]) for s in plan.slots]
    assert layout == [(1, ["a", "b"]), (3, ["loc"]), (4, ["new"])]
    assert plan.first == "a"
    assert plan.unsorted == ("new",)
    a = plan.slots[0].entries[0]
    assert (a.kind, a.weight, a.effort, a.position, a.unsorted, a.health, a.estimate) == (
        "api", "high", "medium", 0, False, None, EST)
    assert plan.slots[1].entries[0].health == "health_unknown"


async def test_dry_run_lists_adapter_unavailable(db, clock):
    make_dep(db, "a", tags=("r",))
    make_dep(db, "broken", tags=("r",), provider="brokenco")
    make_route(db)
    reg = ScriptedRegistry(unavailable_providers={"brokenco": "載入失敗"})
    plan = await router(db, clock, reg).dry_run("r", req())
    assert [(e.deployment, e.reason) for e in plan.excluded] == [("broken", "adapter_unavailable")]
    assert plan.first == "a"


async def test_dry_run_first_follows_rng(db, clock):
    make_dep(db, "a", tags=("r",))
    make_dep(db, "b", tags=("r",))
    make_route(db, targets=[("a", 1, 0), ("b", 1, 1)])
    p0 = await router(db, clock, ScriptedRegistry(), rng=SeqRng([0.0])).dry_run("r", req())
    p1 = await router(db, clock, ScriptedRegistry(), rng=SeqRng([0.99])).dry_run("r", req())
    assert (p0.first, p1.first) == ("a", "b")
    assert [e.deployment for e in p1.slots[0].entries] == ["b", "a"]


async def test_dry_run_unknown_route_raises(db, clock):
    with pytest.raises(RouteNotFound):
        await router(db, clock, ScriptedRegistry()).dry_run("nope", req())


async def test_dry_run_local_direct(db, clock):
    make_dep(db, "loc", kind="local")
    plan = await router(db, clock, ScriptedRegistry()).dry_run("local/loc", req())
    assert plan.route_name is None and plan.first == "loc"
    assert plan.slots[0].number is None and plan.slots[0].entries[0].health == "health_unknown"


# ================================================================== 冷卻到期恢復(4.2.6)


async def test_member_reselected_after_cooldown_expires(db, clock):
    make_dep(db, "a", tags=("r",))
    make_dep(db, "b", tags=("r",))
    make_route(db, targets=[("a", 1, 0), ("b", 2, 0)])
    reg = ScriptedRegistry()
    reg.script = {"a": [fail("rate_limited"), ok()]}
    r = router(db, clock, reg)
    assert (await r.route(req())).served_by == "b"
    assert (await r.route(req())).served_by == "b"            # 仍在冷卻
    clock.advance(seconds=61)
    out = await r.route(req())
    assert out.served_by == "a" and out.slot == 1
    assert reg.called_ids() == ["a", "b", "b", "a"]


# ================================================================== L2:T6 adapter + T5 假端點


KEY = "sk-test-0000"


@pytest.fixture
def real_registry(clock):
    reg = AdapterRegistry(AdapterContext(credentials=credentials.resolve, clock=clock))
    reg.discover()
    return reg


def api_dep(db, fake_api, dep_id, model, *, tags=("r",), efforts=("low", "medium", "high")):
    d = Deployment(id=dep_id, model_name=dep_id, kind="api", provider="openai", model=model,
                   params={"api_base": fake_api.base_url + "/v1"},
                   credential_ref="env:SPILLWAY_TEST_KEY",
                   capabilities={"ctx": 100000, "tools": True, "vision": True,
                                 "efforts": list(efforts)},
                   tags=tags)
    repo.save_deployment(db, d)
    return d


def real_router(db, clock, reg, rng=None):
    return Router(lambda: db, reg, clock, rng or SeqRng([0.0]))


@pytest.fixture
def key_env(monkeypatch):
    monkeypatch.setenv("SPILLWAY_TEST_KEY", KEY)


async def test_l2_full_route_ledger_rows_equal_attempts(db, clock, fake_api, real_registry, key_env):
    api_dep(db, fake_api, "a", "gpt-4o-mini")
    api_dep(db, fake_api, "b", "gpt-4.1-mini")
    make_route(db, targets=[("a", 1, 0), ("b", 2, 0)])
    fake_api.set_mode("gpt-4o-mini", "http_500")
    out = await real_router(db, clock, real_registry).route(req())
    assert out.ok and out.served_by == "b" and out.result.text == "fake upstream ok"
    rows = ledger(db)
    assert len(rows) == len(out.attempts) == 2 == len(fake_api.requests)
    assert [r.status for r in rows] == ["error", "ok"]
    assert payload_count(db) == 2                           # 全域預設記錄 context
    assert rows[1].est_tokens_in is not None and rows[1].est_tokens_out_p90 == 2000


async def test_l2_echo_mode_key_not_in_ledger_or_attempts(db, clock, fake_api, real_registry,
                                                          key_env):
    api_dep(db, fake_api, "a", "gpt-4o-mini")
    api_dep(db, fake_api, "b", "gpt-4.1-mini")
    make_route(db, targets=[("a", 1, 0), ("b", 2, 0)])
    fake_api.set_mode("*", "echo_auth")
    out = await real_router(db, clock, real_registry).route(req())
    assert not out.ok and len(out.attempts) == 2
    for a in out.attempts:
        assert a.status == "error" and a.error and KEY not in a.error
    for r in ledger(db):
        assert r.error and KEY not in r.error
    for (req_json, resp) in db.execute("SELECT request_json, response_text FROM usage_payloads"):
        assert KEY not in req_json and KEY not in (resp or "")


async def test_l2_effort_downgraded_and_upstream_param_matches_effort_map(
        db, clock, fake_api, real_registry, key_env):
    from spillway.adapters.openai_api import MANIFEST
    api_dep(db, fake_api, "a", "gpt-5.4-mini", efforts=("low", "medium", "high"))
    make_route(db)
    out = await real_router(db, clock, real_registry).route(req(reasoning_effort="MAX"))
    assert out.ok
    assert ledger(db)[0].effort == "high"
    body = fake_api.requests[0].body
    assert body["reasoning_effort"] == MANIFEST.effort_map["high"]


async def test_l2_dry_run_does_not_touch_ledger(db, clock, fake_api, real_registry, key_env):
    api_dep(db, fake_api, "a", "gpt-4o-mini")
    api_dep(db, fake_api, "new", "gpt-4.1-mini")
    d = Deployment(id="off", model_name="off", kind="api", provider="openai",
                   model="m", tags=("r",), enabled=False)
    repo.save_deployment(db, d)
    make_route(db, targets=[("a", 1, 0), ("off", 1, 1)])
    r = real_router(db, clock, real_registry)
    await r.route(req())
    before = len(ledger(db))
    plan = await r.dry_run("r", req())
    assert len(ledger(db)) == before == 1
    assert len(fake_api.requests) == 1
    assert {(e.deployment, e.reason) for e in plan.excluded} == {("off", "disabled")}
    assert plan.unsorted == ("new",)
    assert plan.first == "a"


async def test_l2_member_reselected_after_fake_clock_passes_exhausted_until(
        db, clock, fake_api, real_registry, key_env):
    api_dep(db, fake_api, "a", "gpt-4o-mini")
    api_dep(db, fake_api, "b", "gpt-4.1-mini")
    make_route(db, targets=[("a", 1, 0), ("b", 2, 0)])
    r = real_router(db, clock, real_registry)
    fake_api.set_mode("gpt-4o-mini", "http_429")
    out = await r.route(req())
    assert [(a.deployment, a.status) for a in out.attempts] == [("a", "rate_limited"), ("b", "ok")]
    until = repo.get_budget(db, "a").exhausted_until
    assert until == clock.now() + timedelta(seconds=60)
    fake_api.set_mode("gpt-4o-mini", "ok")
    assert (await r.route(req())).served_by == "b"
    clock.set(until + timedelta(seconds=1))
    out = await r.route(req())
    assert out.served_by == "a" and out.slot == 1


def test_route_outcome_ok_property():
    assert RouteOutcome(result=None).ok is False
    assert RouteOutcome(result=ok()).ok is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        RouteOutcome(result=None).served_by = "x"  # type: ignore[misc]


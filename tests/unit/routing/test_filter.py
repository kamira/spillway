"""T11 L1/L2:CandidateFilter(spec 4.2.1–4.2.6;決策 6、14、20)。"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from spillway.adapters.base import AdapterContext, AdapterRegistry
from spillway.domain import Budget, ChatRequest
from spillway.estimate import Estimate, Estimator
from spillway.quota import QuotaService
from spillway.routing import (
    HEALTH_PROBE_TIMEOUT,
    HEALTH_TTL,
    CandidateFilter,
    FilterResult,
    HealthCache,
    Resolver,
)
from spillway.store import repo
from spillway.store.repo import LedgerRow, Route
from tests.contract.fakemods import write_module

from .helpers import StubRegistry, make_dep

pytestmark = pytest.mark.anyio


def req(text: str = "hi", **kw) -> ChatRequest:
    return ChatRequest(model="x", messages=[{"role": "user", "content": text}], **kw)


class FixedEstimator:
    """固定預估;記錄被估的成員,確認 filter 只估一次。"""

    def __init__(self, cost: float | None = 0.01) -> None:
        self.cost = cost
        self.calls: list[tuple[str, str | None]] = []

    def estimate(self, dep, req, route_name):
        self.calls.append((dep.id, route_name))
        return Estimate(tokens_in=10, out_p50=500, out_p90=2000, cost_usd_p90=self.cost)


@pytest.fixture
def stub():
    return StubRegistry()


def make_filter(db, clock, registry, estimator=None, cache=None):
    return CandidateFilter(registry, QuotaService(db, clock), estimator or FixedEstimator(),
                           clock, cache)


def route(db, name="r", *, target=None, mode="chat", precheck=False):
    repo.save_route(db, Route(name, target or {"tags": {"all": [name]}}, mode=mode,
                              budget_precheck=precheck))
    return Resolver(db).resolve(name)


def reasons(res: FilterResult) -> dict[str, str]:
    return {e.deployment_id: e.reason for e in res.excluded}


def cand_ids(res: FilterResult) -> list[str]:
    return [c.deployment.id for c in res.candidates]


# ------------------------------------------------------------------ L1


async def test_disabled_member_excluded(db, clock, stub):
    make_dep(db, "on", tags=("r",))
    make_dep(db, "off", tags=("r",), enabled=False)
    res = await make_filter(db, clock, stub).filter(route(db), req())
    assert cand_ids(res) == ["on"]
    assert reasons(res) == {"off": "disabled"}


async def test_ctx_exceeded_uses_chars_div_4_ceiling(db, clock, stub):
    make_dep(db, "small", tags=("r",), capabilities={"ctx": 2, "tools": True, "vision": True})
    make_dep(db, "fits", tags=("r",), capabilities={"ctx": 3, "tools": True, "vision": True})
    # 9 字元 → ceil(9/4) = 3 token:ctx 2 被篩、ctx 3 剛好不超過
    res = await make_filter(db, clock, stub).filter(route(db), req("123456789"))
    assert cand_ids(res) == ["fits"]
    assert reasons(res) == {"small": "ctx_exceeded"}


async def test_missing_ctx_is_not_limited(db, clock, stub):
    make_dep(db, "noctx", tags=("r",), capabilities={"tools": True, "vision": True})
    res = await make_filter(db, clock, stub).filter(route(db), req("x" * 100000))
    assert cand_ids(res) == ["noctx"]


async def test_tools_request_excludes_tools_false(db, clock, stub):
    make_dep(db, "t", tags=("r",), capabilities={"ctx": 1000, "tools": True, "vision": False})
    make_dep(db, "nt", tags=("r",), capabilities={"ctx": 1000, "tools": False, "vision": False})
    make_dep(db, "unk", tags=("r",), capabilities={"ctx": 1000})
    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    res = await make_filter(db, clock, stub).filter(route(db), req(tools=tools))
    assert cand_ids(res) == ["t"]
    assert reasons(res) == {"nt": "tools_unsupported", "unk": "tools_unsupported"}
    # 不含 tools 時不看 tools 能力
    res2 = await make_filter(db, clock, stub).filter(route(db), req())
    assert cand_ids(res2) == ["nt", "t", "unk"]


async def test_image_request_excludes_vision_false(db, clock, stub):
    make_dep(db, "v", tags=("r",), capabilities={"ctx": 1000, "tools": True, "vision": True})
    make_dep(db, "nv", tags=("r",), capabilities={"ctx": 1000, "tools": True, "vision": False})
    img = ChatRequest(model="x", messages=[{"role": "user", "content": [
        {"type": "text", "text": "看圖"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}])
    res = await make_filter(db, clock, stub).filter(route(db), img)
    assert cand_ids(res) == ["v"]
    assert reasons(res) == {"nv": "vision_unsupported"}


async def test_chat_route_excludes_cli(db, clock, stub):
    make_dep(db, "sub", kind="cli", tags=("r",))
    make_dep(db, "api", kind="api", tags=("r",))
    res = await make_filter(db, clock, stub).filter(route(db), req())
    assert cand_ids(res) == ["api"]
    assert reasons(res) == {"sub": "mode_chat_excludes_cli"}


async def test_task_route_keeps_cli(db, clock, stub):
    make_dep(db, "sub", kind="cli", tags=("r",))
    res = await make_filter(db, clock, stub).filter(route(db, mode="task"), req())
    assert cand_ids(res) == ["sub"]


async def test_metadata_mode_task_counts_as_task(db, clock, stub):
    make_dep(db, "sub", kind="cli", tags=("r",))
    res = await make_filter(db, clock, stub).filter(route(db), req(metadata={"mode": "task"}))
    assert cand_ids(res) == ["sub"] and res.excluded == ()


async def test_quota_exhausted_when_remaining_le_zero(db, clock, stub):
    make_dep(db, "zero", tags=("r",), budget=Budget("usd", 0.0, "daily"))
    make_dep(db, "spent", tags=("r",), budget=Budget("usd", 1.0, "daily"))
    make_dep(db, "nolimit", tags=("r",), budget=Budget("usd", None, "daily"))  # 決策 6
    make_dep(db, "nobudget", tags=("r",))
    repo.insert_ledger(db, LedgerRow(ts=clock.now(), route_name="r", deployment_id="spent",
                                     status="ok", cost_usd=1.0))
    res = await make_filter(db, clock, stub).filter(route(db), req())
    assert cand_ids(res) == ["nobudget", "nolimit"]
    assert reasons(res) == {"zero": "quota_exhausted", "spent": "quota_exhausted"}


async def test_cooling_member_excluded_and_recovers(db, clock, stub):
    make_dep(db, "hot", tags=("r",), budget=Budget("usd", 10.0, "daily",
                                                     exhausted_until=clock.now() + timedelta(seconds=60)))
    f = make_filter(db, clock, stub)
    t = route(db)
    assert reasons(await f.filter(t, req())) == {"hot": "cooling"}
    clock.advance(seconds=61)
    assert cand_ids(await f.filter(t, req())) == ["hot"]


async def test_local_skips_quota_and_cooling(db, clock, stub):
    make_dep(db, "loc", kind="local", tags=("r",),
             budget=Budget("usd", 0.0, "daily", exhausted_until=clock.now() + timedelta(hours=1)))
    res = await make_filter(db, clock, stub).filter(route(db), req())
    assert cand_ids(res) == ["loc"]


async def test_unhealthy_local_excluded(db, clock, stub):
    make_dep(db, "loc", kind="local", tags=("r",))
    stub.healthy["loc"] = False
    res = await make_filter(db, clock, stub).filter(route(db), req())
    assert reasons(res) == {"loc": "unhealthy"}


async def test_health_exception_counts_as_unhealthy(db, clock, stub):
    make_dep(db, "loc", kind="local", tags=("r",))
    stub.healthy["loc"] = RuntimeError("boom")
    res = await make_filter(db, clock, stub).filter(route(db), req())
    assert reasons(res) == {"loc": "unhealthy"}


async def test_adapter_unavailable_from_registry(db, clock):
    reg = StubRegistry(unavailable_providers={"brokenco": "模組 broken 載入失敗"})
    make_dep(db, "b", tags=("r",), provider="brokenco")
    make_dep(db, "ok", tags=("r",))
    res = await make_filter(db, clock, reg).filter(route(db), req())
    assert cand_ids(res) == ["ok"]
    assert reasons(res) == {"b": "adapter_unavailable"}


async def test_candidates_carry_estimate_computed_once(db, clock, stub):
    make_dep(db, "a", tags=("r",))
    make_dep(db, "loc", kind="local", tags=("r",))
    est = FixedEstimator()
    res = await make_filter(db, clock, stub, est).filter(route(db), req())
    assert [c.estimate for c in res.candidates] == [Estimate(10, 500, 2000, 0.01)] * 2
    assert est.calls == [("a", "r"), ("loc", "r")]
    assert [c.health for c in res.candidates] == [None, "healthy"]


async def test_implicit_route_estimates_with_model_name(db, clock, stub):
    make_dep(db, "a", model_name="gpt")
    est = FixedEstimator()
    await make_filter(db, clock, stub, est).filter(Resolver(db).resolve("gpt"), req())
    assert est.calls == [("a", "gpt")]


# ------------------------------------------------------------------ L2:額度(T7)與預估(T9)


async def test_precheck_skips_when_p90_exceeds_remaining(db, clock, stub):
    make_dep(db, "poor", tags=("r",), budget=Budget("usd", 0.005, "daily"))
    make_dep(db, "rich", tags=("r",), budget=Budget("usd", 1.0, "daily"))
    make_dep(db, "free", tags=("r",))                    # 無上限:剩餘無限
    make_dep(db, "loc", kind="local", tags=("r",), budget=Budget("usd", 0.001, "daily"))
    res = await make_filter(db, clock, stub, FixedEstimator(0.01)).filter(
        route(db, precheck=True), req())
    assert cand_ids(res) == ["free", "loc", "rich"]
    assert reasons(res) == {"poor": "skipped_precheck"}
    skipped = res.excluded[0]
    assert skipped.estimate == Estimate(10, 500, 2000, 0.01)   # T12 寫帳本用
    assert skipped.deployment.id == "poor"


async def test_precheck_off_does_not_skip(db, clock, stub):
    make_dep(db, "poor", tags=("r",), budget=Budget("usd", 0.005, "daily"))
    res = await make_filter(db, clock, stub, FixedEstimator(0.01)).filter(route(db), req())
    assert cand_ids(res) == ["poor"]


async def test_precheck_ignores_unpriced_estimate(db, clock, stub):
    make_dep(db, "poor", tags=("r",), budget=Budget("usd", 0.005, "daily"))
    res = await make_filter(db, clock, stub, FixedEstimator(None)).filter(
        route(db, precheck=True), req())
    assert cand_ids(res) == ["poor"]


async def test_precheck_with_real_estimator_and_quota(db, clock, stub):
    # 真實 Estimator(T9):max_tokens 固定 P90,gpt-4o-mini 在本地價格表內;剩餘 = 上限 − 本期花費(T7)
    make_dep(db, "a", tags=("r",), model="gpt-4o-mini", budget=Budget("usd", 1.0, "daily"))
    est = Estimator(db).estimate(repo.load_deployment(db, "a"), req(max_tokens=1000), "r")
    assert est.cost_usd_p90 is not None and est.cost_usd_p90 > 0
    q = QuotaService(db, clock)
    repo.insert_ledger(db, LedgerRow(ts=clock.now(), route_name="r", deployment_id="a",
                                     status="ok", cost_usd=1.0 - est.cost_usd_p90 / 2))
    assert 0 < q.remaining(repo.load_deployment(db, "a")) < est.cost_usd_p90
    f = CandidateFilter(stub, q, Estimator(db), clock)
    res = await f.filter(route(db, precheck=True), req(max_tokens=1000))
    assert reasons(res) == {"a": "skipped_precheck"}
    res2 = await f.filter(route(db, precheck=True), req(max_tokens=1))
    assert cand_ids(res2) == ["a"]
    assert res2.candidates[0].estimate.out_p90 == 1


async def test_cooling_set_by_quota_mark_is_read(db, clock, stub):
    dep = make_dep(db, "a", tags=("r",), budget=Budget("usd", 5.0, "daily"))
    q = QuotaService(db, clock)
    q.mark(dep, "rate_limited")
    f = CandidateFilter(stub, q, FixedEstimator(), clock)
    assert reasons(await f.filter(route(db), req())) == {"a": "cooling"}
    clock.advance(seconds=61)
    assert cand_ids(await f.filter(route(db), req())) == ["a"]


async def test_real_registry_unavailable_provider(db, clock, tmp_path):
    write_module(tmp_path / "mods", "broken_mod", pre_import="import spillway_no_such_pkg\n",
                 providers=("brokenco",))
    reg = AdapterRegistry(AdapterContext(credentials=lambda ref: "x", clock=clock))
    reg.discover(extra_paths=[tmp_path / "mods"])
    assert "brokenco" in reg.unavailable()
    make_dep(db, "b", tags=("r",), provider="brokenco")
    make_dep(db, "fallback", tags=("r",), provider="some-unknown-api")  # 由 litellm 後備處理
    res = await make_filter(db, clock, reg).filter(route(db), req())
    assert reasons(res) == {"b": "adapter_unavailable"}
    assert cand_ids(res) == ["fallback"]


# ------------------------------------------------------------------ 決策 20:健康快取


class _HangingHealth:
    """health() 等一個永不觸發的 Event(模擬沒有自設逾時的第三方模組)。"""

    def __init__(self) -> None:
        self.never = asyncio.Event()
        self.calls = 0

    def module_for(self, dep):
        return object()

    def get(self, dep):
        return self

    async def health(self, dep) -> bool:
        self.calls += 1
        await self.never.wait()
        return True


async def test_health_probe_timeout_counts_as_unhealthy_and_is_cached(db, clock):
    # M17:決策 20 的 2 秒上限由 filter 強制,不依賴模組自己的逾時
    assert HEALTH_PROBE_TIMEOUT == 2.0
    make_dep(db, "loc", kind="local", tags=("r",))
    reg = _HangingHealth()
    cache = HealthCache(clock)
    f = CandidateFilter(reg, QuotaService(db, clock), FixedEstimator(), clock, cache,
                        health_timeout=0.05)
    res = await asyncio.wait_for(f.filter(route(db), req()), timeout=2)
    assert reasons(res) == {"loc": "unhealthy"}
    assert cache.get("loc") is False                     # 逾時結果已寫入快取
    await f.filter(route(db), req())
    assert reg.calls == 1                                # 快取有效期間不再探測


async def test_health_cached_for_ttl_via_clock(db, clock, stub):
    make_dep(db, "loc", kind="local", tags=("r",))
    f = make_filter(db, clock, stub, cache=HealthCache(clock))
    t = route(db)
    await f.filter(t, req())
    await f.filter(t, req())
    assert stub.health_calls == ["loc"]
    clock.advance(seconds=HEALTH_TTL.total_seconds() - 1)
    stub.healthy["loc"] = False
    assert cand_ids(await f.filter(t, req())) == ["loc"]       # 快取仍有效
    clock.advance(seconds=1)
    assert reasons(await f.filter(t, req())) == {"loc": "unhealthy"}
    assert stub.health_calls == ["loc", "loc"]
    assert HEALTH_TTL == timedelta(seconds=10)


async def test_dry_run_cold_cache_does_not_probe(db, clock, stub):
    make_dep(db, "loc", kind="local", tags=("r",))
    stub.healthy["loc"] = False
    cache = HealthCache(clock)
    f = make_filter(db, clock, stub, cache=cache)
    res = await f.filter(route(db), req(), probe=False)
    assert stub.health_calls == []
    assert cand_ids(res) == ["loc"]
    assert res.candidates[0].health == "health_unknown"
    # 暖快取後 dry-run 讀快取結果
    await f.filter(route(db), req())
    res2 = await f.filter(route(db), req(), probe=False)
    assert reasons(res2) == {"loc": "unhealthy"}
    assert stub.health_calls == ["loc"]


async def test_real_registry_health_probes_fake_upstream(db, clock, fake_api):
    reg = AdapterRegistry(AdapterContext(credentials=lambda ref: "x", clock=clock))
    reg.discover()
    make_dep(db, "up", kind="local", tags=("r",), params={"api_base": fake_api.base_url + "/v1"})
    make_dep(db, "down", kind="local", tags=("r",), params={"api_base": "http://127.0.0.1:9/v1"})
    res = await make_filter(db, clock, reg).filter(route(db), req())
    assert cand_ids(res) == ["up"]
    assert reasons(res) == {"down": "unhealthy"}


async def test_health_cache_unit(clock):
    c = HealthCache(clock)
    assert c.get("x") is None
    c.put("x", True)
    assert c.get("x") is True
    clock.advance(seconds=10)
    assert c.get("x") is None

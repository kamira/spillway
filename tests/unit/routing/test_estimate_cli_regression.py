"""CHG-20261101-03 T6:CLI 成員估算回歸(決策 13「估算維持現況,本筆只補回歸測試」)。

- `cli_overhead_tokens`(spec 5.4.2)只加在 kind=cli;不合法值視為 0。
- codex-cli 花費預估一律 NULL(5.4.4,`NO_PRICE_PROVIDERS`),所以預算預檢不會跳過它。
- claude-cli 查價順序 `claude-cli/<model>` 再裸 `model`:LiteLLM 價格表有的模型可得非 NULL 預估。
"""
from __future__ import annotations

import pytest

from spillway.domain import Budget, ChatRequest, Deployment
from spillway.estimate import NO_PRICE_PROVIDERS, Estimator
from spillway.quota import QuotaService
from spillway.routing import CandidateFilter, Resolver
from spillway.store.repo import Route, save_route

from .helpers import make_dep
from .router_helpers import ScriptedRegistry

pytestmark = pytest.mark.anyio

REQ = ChatRequest(model="r", messages=[{"role": "user", "content": "hello there"}], max_tokens=100,
                  metadata={"mode": "task"})


def _dep(kind: str, provider: str, model: str, **params) -> Deployment:
    return Deployment(id=f"d-{provider}", model_name="m", kind=kind, provider=provider, model=model,
                      params=params)


@pytest.mark.parametrize("value", [-5, "100", True, 1.5, None])
def test_invalid_cli_overhead_is_ignored(db, value):
    e = Estimator(db)
    base = _dep("cli", "claude-cli", "claude-sonnet-4-5")
    bad = _dep("cli", "claude-cli", "claude-sonnet-4-5", cli_overhead_tokens=value)
    assert e.estimate(bad, REQ, "r").tokens_in == e.estimate(base, REQ, "r").tokens_in


def test_cli_overhead_only_for_cli_kind(db):
    e = Estimator(db)
    api = _dep("api", "anthropic", "claude-sonnet-4-5")
    api_oh = _dep("api", "anthropic", "claude-sonnet-4-5", cli_overhead_tokens=999)
    assert e.estimate(api_oh, REQ, "r").tokens_in == e.estimate(api, REQ, "r").tokens_in
    codex = _dep("cli", "codex-cli", "gpt-5")
    codex_oh = _dep("cli", "codex-cli", "gpt-5", cli_overhead_tokens=999)
    assert e.estimate(codex_oh, REQ, "r").tokens_in == e.estimate(codex, REQ, "r").tokens_in + 999


def test_codex_cost_is_null_even_for_priced_model(db):
    assert NO_PRICE_PROVIDERS == frozenset({"codex-cli"})
    est = Estimator(db).estimate(_dep("cli", "codex-cli", "gpt-4o-mini"), REQ, "r")
    assert est.cost_usd_p90 is None and est.tokens_in > 0


def test_claude_cli_priced_by_bare_model(db):
    est = Estimator(db).estimate(_dep("cli", "claude-cli", "claude-sonnet-4-5"), REQ, "r")
    assert est.cost_usd_p90 is not None and est.cost_usd_p90 > 0


async def test_precheck_never_skips_codex_member(db, clock):
    make_dep(db, "codex", kind="cli", provider="codex-cli", model="gpt-5", tags=("r",),
             budget=Budget("credit_pool", 0.000001, "monthly"))
    save_route(db, Route("r", {"tags": {"all": ["r"]}}, mode="task", budget_precheck=True))
    f = CandidateFilter(ScriptedRegistry(), QuotaService(db, clock), Estimator(db), clock)
    res = await f.filter(Resolver(db).resolve("r"), REQ)
    assert [c.deployment.id for c in res.candidates] == ["codex"]
    assert res.candidates[0].estimate.cost_usd_p90 is None

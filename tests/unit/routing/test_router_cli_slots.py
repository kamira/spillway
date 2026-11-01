"""CHG-20261101-03 T6 L1:CLI 併發名額、停機旗標、成員 env 憑證的移除與遮罩(決策 7、9、16)。

名額一律用真的 `CliRunner`(`try_acquire`/`release`/`in_flight` 為同步、不起子行程);帳本、額度用真的
`LedgerWriter`、`QuotaService` 與 tmp_path 資料庫。
"""
from __future__ import annotations

import asyncio
import dataclasses
import sys
from datetime import timedelta

import pytest

from spillway.adapters.base import CliRunner
from spillway.domain import CallResult, ChatRequest, Deployment
from spillway.quota import QuotaService
from spillway.redact import REDACTOR
from spillway.routing import CandidateFilter, Resolver
from spillway.routing.filter import DEFAULT_MAX_CONCURRENCY, max_concurrency
from spillway.routing.router import Router
from spillway.clock import SeqRng
from spillway.store import repo
from spillway.store.repo import Route

from .helpers import make_dep
from .router_helpers import FixedEstimator, ScriptedRegistry, fail, ok

pytestmark = pytest.mark.anyio

MSGS = [{"role": "user", "content": "hi"}]
TASK = {"mode": "task"}
SECRET_A = "plainsecret-t6-a1b2c3d4"      # 不符合預設遮罩樣式(sk-、Bearer)的值
SECRET_B = "plainsecret-t6-e5f6g7h8"


def req(model: str = "r", **kw) -> ChatRequest:
    return ChatRequest(model=model, messages=MSGS, metadata=dict(TASK), **kw)


def make_route(db, name="r", *, targets=(), record_context=True):
    repo.save_route(db, Route(name, {"tags": {"all": [name]}}, mode="task",
                              record_context=record_context))
    if targets:
        repo.replace_route_targets(db, name, [repo.RouteTarget(*t) for t in targets])


def router(db, clock, reg, runner, **kw) -> Router:
    return Router(lambda: db, reg, clock, SeqRng([0.0]), estimator=FixedEstimator(),
                  cli_runner=runner, **kw)


def ledger(db):
    return list(reversed(repo.list_ledger(db)))


def cli_dep(db, dep_id="sub", **kw):
    return make_dep(db, dep_id, kind="cli", tags=("r",), **kw)


def with_cred(db, dep: Deployment, ref: str) -> Deployment:
    dep = dataclasses.replace(dep, credential_ref=ref)
    repo.save_deployment(db, dep)
    return dep


class LosingRunner(CliRunner):
    """篩選時看起來有名額,但 `try_acquire` 被並行請求搶走(決策 7)。"""

    def try_acquire(self, key: str, limit: int) -> bool:
        return False


class HookRegistry(ScriptedRegistry):
    """`call()` 改走 `hooks[dep_id]`(async 函式),沒有 hook 時照腳本。"""

    def __init__(self) -> None:
        super().__init__()
        self.hooks: dict = {}
        outer = self
        base_call = self._adapter.call

        async def call(dep, r, effort):
            hook = outer.hooks.get(dep.id)
            if hook is None:
                return await base_call(dep, r, effort)
            outer.calls.append((dep.id, effort, r))
            return await hook(dep)

        self._adapter.call = call  # type: ignore[method-assign]


# ================================================================== max_concurrency


@pytest.mark.parametrize(("params", "want"), [
    ({}, 1), ({"max_concurrency": 3}, 3), ({"max_concurrency": 0}, 1),
    ({"max_concurrency": "2"}, 1), ({"max_concurrency": True}, 1), ({"max_concurrency": 1.5}, 1),
])
def test_max_concurrency_param(params, want):
    dep = Deployment(id="x", model_name="x", kind="cli", provider="claude-cli", model="m",
                     params=params)
    assert max_concurrency(dep) == want
    assert DEFAULT_MAX_CONCURRENCY == 1


# ================================================================== CandidateFilter


def _filter(db, clock, runner):
    return CandidateFilter(ScriptedRegistry(), QuotaService(db, clock), FixedEstimator(), clock,
                           cli_runner=runner)


async def test_filter_excludes_cli_member_at_capacity(db, clock):
    cli_dep(db, "sub")
    cli_dep(db, "wide", params={"max_concurrency": 2})
    make_dep(db, "api", tags=("r",))
    make_route(db)
    runner = CliRunner()
    for key in ("sub", "wide", "api"):
        assert runner.try_acquire(key, 5)
    res = await _filter(db, clock, runner).filter(Resolver(db).resolve("r"), req())
    assert res.reasons() == {"sub": "concurrency_full"}
    assert sorted(c.deployment.id for c in res.candidates) == ["api", "wide"]
    assert runner.try_acquire("wide", 5)
    res = await _filter(db, clock, runner).filter(Resolver(db).resolve("r"), req())
    assert res.reasons() == {"sub": "concurrency_full", "wide": "concurrency_full"}
    assert ledger(db) == []


async def test_filter_cooling_precedes_concurrency_full(db, clock):
    cli_dep(db, "sub")
    make_route(db)
    repo.mark_cooldown(db, "sub", clock.now() + timedelta(hours=1), "rate_limited")
    runner = CliRunner()
    runner.try_acquire("sub", 1)
    res = await _filter(db, clock, runner).filter(Resolver(db).resolve("r"), req())
    assert res.reasons() == {"sub": "cooling"}


async def test_filter_without_runner_does_not_check_concurrency(db, clock):
    cli_dep(db, "sub")
    make_route(db)
    res = await _filter(db, clock, None).filter(Resolver(db).resolve("r"), req())
    assert [c.deployment.id for c in res.candidates] == ["sub"]


async def test_dry_run_shows_concurrency_full(db, clock):
    cli_dep(db, "sub")
    make_dep(db, "api", tags=("r",))
    make_route(db)
    runner = CliRunner()
    runner.try_acquire("sub", 1)
    plan = await router(db, clock, ScriptedRegistry(), runner).dry_run("r", req())
    assert [(e.deployment, e.reason) for e in plan.excluded] == [("sub", "concurrency_full")]
    assert plan.first == "api"


# ================================================================== Router 名額


async def test_lost_race_is_excluded_without_ledger_row(db, clock):
    cli_dep(db, "sub")
    make_dep(db, "api", tags=("r",))
    make_route(db)
    reg = ScriptedRegistry()
    out = await router(db, clock, reg, LosingRunner()).route(req())
    assert out.served_by == "api"
    assert out.excluded == (("sub", "concurrency_full"),)
    assert reg.called_ids() == ["api"]
    (row,) = ledger(db)
    assert (row.deployment_id, row.fallback_depth) == ("api", 0)


async def test_slot_held_during_call_and_released_after_success(db, clock):
    cli_dep(db, "sub")
    make_route(db)
    runner = CliRunner()
    reg = HookRegistry()
    seen = []

    async def hook(dep):
        seen.append(runner.in_flight("sub"))
        return ok("done")

    reg.hooks["sub"] = hook
    out = await router(db, clock, reg, runner).route(req())
    assert out.served_by == "sub" and seen == [1]
    assert runner.in_flight("sub") == 0


@pytest.mark.parametrize("outcome", ["fail", "raise"])
async def test_slot_released_after_failure_or_exception(db, clock, outcome):
    cli_dep(db, "sub")
    make_dep(db, "api", tags=("r",))
    make_route(db)
    runner = CliRunner()
    reg = ScriptedRegistry()
    reg.script = {"sub": fail() if outcome == "fail" else RuntimeError("adapter bug")}
    out = await router(db, clock, reg, runner).route(req())
    assert out.served_by == "api"
    assert runner.in_flight("sub") == 0
    assert [r.deployment_id for r in ledger(db)] == ["sub", "api"]


async def test_api_and_local_members_never_take_slots(db, clock):
    make_dep(db, "api", tags=("r",))
    make_route(db)

    class Strict(CliRunner):
        def try_acquire(self, key, limit):
            raise AssertionError("api 成員不可取名額")

    out = await router(db, clock, ScriptedRegistry(), Strict()).route(req())
    assert out.served_by == "api"


async def _cancel_inflight(db, clock, *, shutting_down: bool):
    cli_dep(db, "sub")
    make_route(db)
    runner = CliRunner()
    reg = HookRegistry()
    started = asyncio.Event()

    async def hook(dep):
        started.set()
        await asyncio.Event().wait()

    reg.hooks["sub"] = hook
    rt = router(db, clock, reg, runner)
    task = asyncio.ensure_future(rt.route(req()))
    await started.wait()
    assert runner.in_flight("sub") == 1
    if shutting_down:
        rt.shutting_down = True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner.in_flight("sub") == 0
    (row,) = ledger(db)
    return row


async def test_cancel_releases_slot_and_records_cancelled(db, clock):
    row = await _cancel_inflight(db, clock, shutting_down=False)
    assert (row.status, row.error) == ("error", "cancelled")


async def test_cancel_while_shutting_down_records_shutdown(db, clock):
    row = await _cancel_inflight(db, clock, shutting_down=True)
    assert (row.status, row.error) == ("error", "shutdown")


# ================================================================== 停機旗標(決策 16)


async def test_shutting_down_starts_no_attempt(db, clock):
    cli_dep(db, "sub")
    make_dep(db, "api", tags=("r",))
    make_route(db)
    flag = {"on": True}
    reg = ScriptedRegistry()
    rt = router(db, clock, reg, CliRunner(), shutdown_flag=lambda: flag["on"])
    assert rt.shutting_down is True
    out = await rt.route(req())
    assert not out.ok and out.attempts == ()
    assert sorted(out.excluded) == [("api", "shutting_down"), ("sub", "shutting_down")]
    assert reg.calls == [] and ledger(db) == []
    flag["on"] = False
    assert (await rt.route(req())).ok


async def test_shutdown_between_attempts_stops_fallback(db, clock):
    make_dep(db, "a", tags=("r",))
    make_dep(db, "b", tags=("r",))
    make_route(db, targets=[("a", 1, 0), ("b", 2, 0)])
    reg = HookRegistry()
    rt = router(db, clock, reg, CliRunner())

    async def hook(dep):
        rt.shutting_down = True
        return fail()

    reg.hooks["a"] = hook
    out = await rt.route(req())
    assert not out.ok
    assert reg.called_ids() == ["a"]
    assert out.excluded == (("b", "shutting_down"),)
    assert [r.deployment_id for r in ledger(db)] == ["a"]


async def test_shutting_down_direct_local_is_no_candidate(db, clock):
    make_dep(db, "loc", kind="local")
    reg = ScriptedRegistry()
    rt = router(db, clock, reg, CliRunner())
    rt.shutting_down = True
    out = await rt.route(ChatRequest(model="local/loc", messages=MSGS))
    assert not out.ok and out.excluded == (("loc", "shutting_down"),)
    assert reg.calls == [] and ledger(db) == []


def test_shutdown_flag_errors_read_as_false(db, clock):
    def boom():
        raise RuntimeError("x")

    assert router(db, clock, ScriptedRegistry(), None, shutdown_flag=boom).shutting_down is False


# ================================================================== 成員 env 憑證(決策 9)


async def test_cli_attempt_removes_all_member_env_credentials_and_masks_values(
        db, clock, monkeypatch):
    monkeypatch.setenv("SPILLWAY_T6_A", SECRET_A)
    monkeypatch.setenv("SPILLWAY_T6_B", f"  {SECRET_B}\n")
    monkeypatch.delenv("SPILLWAY_T6_UNSET", raising=False)
    sub = cli_dep(db, "sub")
    with_cred(db, make_dep(db, "a", tags=("other",)), "env:SPILLWAY_T6_A")
    with_cred(db, make_dep(db, "b", tags=("other",), enabled=False), "env:SPILLWAY_T6_B")
    with_cred(db, make_dep(db, "u", tags=("other",)), "env:SPILLWAY_T6_UNSET")
    with_cred(db, make_dep(db, "k", tags=("other",)), "keyring:svc:acct")
    make_dep(db, "api", tags=("r",))
    make_route(db, targets=[("sub", 1, 0), ("api", 2, 0)])
    assert sub.credential_ref is None
    assert not REDACTOR.redact(SECRET_A)[1]     # 預設樣式遮不到:遮罩必須來自 extra_secrets

    runner = CliRunner()
    reg = ScriptedRegistry()
    reg.script = {"sub": [fail(error=f"upstream echoed {SECRET_A} and {SECRET_B}")],
                  "api": ok("fine")}
    out = await router(db, clock, reg, runner).route(req())
    assert runner.extra_env_remove == {"SPILLWAY_T6_A", "SPILLWAY_T6_B", "SPILLWAY_T6_UNSET"}
    assert out.served_by == "api"
    info = out.attempts[0]
    assert info.deployment == "sub" and SECRET_A not in info.error and SECRET_B not in info.error
    row = ledger(db)[0]
    assert row.deployment_id == "sub"
    assert SECRET_A not in row.error and SECRET_B not in row.error

    db.execute("DELETE FROM usage_ledger")
    reg.script = {"sub": ok(f"leaked {SECRET_A} / {SECRET_B}")}
    out = await router(db, clock, reg, runner).route(req())
    assert out.served_by == "sub"
    (row,) = ledger(db)
    p = repo.get_payload(db, row.id)
    assert SECRET_A not in p.response_text and SECRET_B not in p.response_text
    assert p.redacted


async def test_api_attempt_does_not_touch_extra_env_remove(db, clock, monkeypatch):
    monkeypatch.setenv("SPILLWAY_T6_A", SECRET_A)
    with_cred(db, make_dep(db, "api", tags=("r",)), "env:SPILLWAY_T6_A")
    make_route(db)
    runner = CliRunner()
    await router(db, clock, ScriptedRegistry(), runner).route(req())
    assert runner.extra_env_remove == set()


async def test_cli_runner_merges_extra_env_remove(monkeypatch, tmp_path):
    monkeypatch.setenv("SPILLWAY_T6_A", SECRET_A)
    monkeypatch.setenv("SPILLWAY_T6_KEEP", "kept")
    runner = CliRunner()
    runner.extra_env_remove = {"SPILLWAY_T6_A"}
    code = ("import os;print(os.environ.get('SPILLWAY_T6_A','<none>'));"
            "print(os.environ.get('SPILLWAY_T6_KEEP','<none>'))")
    out = await runner.run([sys.executable, "-I", "-c", code], cwd=tmp_path, timeout=30)
    assert out.stdout.split() == ["<none>", "kept"]

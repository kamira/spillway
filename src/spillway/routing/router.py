"""Router:單次請求的完整路由(spec 4.5、5.4.1、5.4.5、6.1、6.2;CHG-20261030-02 T12;決策 18、19、20、29)。

`Router(conn_factory, registry, clock, rng, *, quota=None, estimator=None, ledger=None,
health_cache=None, close_conn=False)`

- `await route(req) -> RouteOutcome`:解析 → 篩選(probe=True)→ 排序 → 逐 slot 選擇並呼叫。
- `await dry_run(route_name_or_model, req) -> Plan`:同一管線到 SlotSelector 為止(probe=False);
  不呼叫 adapter、不寫帳本、不設冷卻。
- `normalize_effort(value)`:請求 `reasoning_effort` 正規化(決策 29);定義在 `spillway.effort`,
  此處轉匯出(CHG-20261101-02 決策 8)。
- 回傳型別 `RouteOutcome`、`AttemptInfo`、`Plan`、`PlanSlot`、`PlanEntry`、`PlanExcluded` 定義在
  `spillway.routing.outcome`,此處轉匯出(CHG-20261101-02 決策 10)。

連線(design D2「每請求一連線」;施工審議波次 3 建議修):
    `conn_factory` 是零參數函式,回傳 `sqlite3.Connection`。每次 `route()` / `dry_run()` 只呼叫
    一次,這個連線供本次請求的 Resolver、QuotaService、Estimator、LedgerWriter 共用(以
    `contextvars` 保存,並行請求各用各的)。`close_conn=True` 時請求結束後關閉該連線(工廠每次
    開新連線的情況);預設 False(工廠回傳共用連線,例如測試的 `lambda: db`)。
    `quota`、`estimator`、`ledger` 可注入;未給時以「本次請求的連線」建立。
    內建元件(`router.quota`、`router.estimator`、`router.resolver`)只在 `route()`/`dry_run()`
    期間可用:在那之外取連線會丟 `RuntimeError`,不會另開連線(施工審議 S6,避免連線洩漏)。`ledger` 未給時每次
    請求建一個 `LedgerWriter(conn)`(它只接受連線)。`health_cache` 跨請求共用(決策 20)。

route() 規則:
1. `normalize_effort(req.reasoning_effort)`;不合法丟 `InvalidEffort`(T13 轉 422)。
2. 解析失敗丟 resolver 的 `RouteNotFound`(T13 轉 404)。
3. `local/` 直呼(4.1.1、決策 18):不篩選、不探測健康、不 fallback;只呼叫該成員一次。
   帳本 `slot`、`route_name` 為 NULL,`RouteOutcome.slot` 為 None(T13 的 header 為 `direct`)。
   成員停用時不呼叫、不寫帳本,回 no_candidate,`attempts` 一筆 status `disabled`;
   provider 沒有可用模組時同理,status `adapter_unavailable`。
4. route / 隱含 route:
   - 先為每個 `skipped_precheck` 成員寫一列帳本(4.2.5;`request_text=None` 不寫 payload,
     est_* 取 `Excluded.estimate`,`slot` 取該成員若未被跳過時會落在的 slot),並列入 attempts。
   - 依 slot(編號小者先)以 `SlotSelector.iterate` 逐一嘗試;成功即回傳,失敗取下一位(4.4.3–4.4.5)。
5. 每次到達 adapter 的嘗試:
   - effort = `EffortMapper.pick(slot entry 的 effort, 正規化後的請求 effort, capabilities.efforts)`;
     capabilities 中的等級不合法時視為不傳(None)。
   - `extra_secrets` = `credentials.resolve(dep.credential_ref)` 的值;未設定或解析失敗時為空
     (adapter 自己會回 error)。
   - `await registry.get(dep).call(dep, req, effort)`;丟例外時視為 `status=error`(訊息只含例外型別
     與訊息,經遮罩)。status 不在 `CALL_STATUSES` 時記為 `error`。
   - 帳本以 `Attempt` 一次寫入(決策 19):`request_text` = `req.messages` 的 JSON;`response_text` =
     回應文字,有 `tool_calls` 時為 `{"content", "tool_calls", "finish_reason"}` 的 JSON;失敗且沒有文字時
     為 None。est_* 取 `Candidate.estimate`(filter 已算過,不重算)。`route_record_context` = route 的
     `record_context`(隱含 route 與直呼為 None,沿用全域)。
   - `fallback_depth` = 本次請求中在它之前已到達 adapter 的嘗試數(第一次為 0;被篩掉、冷卻中、
     `skipped_precheck` 的成員不算——它們沒有被呼叫)。`skipped_precheck` 列的 fallback_depth 為 0。
   - `latency_ms` 以 `time.perf_counter()` 量測 adapter 呼叫耗時(延遲量測不屬「業務時間」,
     帳本 `ts` 仍取注入的 Clock)。
   - `quota_exhausted` / `rate_limited` 且成員為 cli/api → `QuotaService.mark()`(4.5.2)。
     local 成員不設冷卻(4.2.4 不檢查 local 的額度與冷卻,設了也不生效)。
     `mark()` 丟例外時只以 logging 記錄例外型別(不記內容與秘密),帳本照寫原 status,照常換下一位
     (施工審議 M18;4.5.1、4.4.3)。
   - adapter 呼叫中被取消(`CancelledError`)時,同步寫一列 `status=error`、`error="cancelled"`
     (不跨 await),再重新丟出(施工審議 S2;上游可能已計費)。
6. 全部失敗 → `RouteOutcome(result=None, attempts=...)`;每筆 `AttemptInfo.error` 以該次的
   `extra_secrets` 經 Redactor 遮罩(帳本由 LedgerWriter 自行遮罩)。`excluded` 另列被篩掉的成員與原因。
"""
from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from spillway import credentials
from spillway.adapters.base import AdapterUnavailableError
from spillway.clock import Clock, Rng
from spillway.domain import (
    CALL_STATUSES,
    CLOUD_KINDS,
    COOLING_STATUSES,
    STATUS_SKIPPED_PRECHECK,
    CallResult,
    ChatRequest,
    Deployment,
)
from spillway.effort import EffortMapper, InvalidEffort, normalize_effort
from spillway.estimate import Estimate, Estimator
from spillway.ledger import Attempt, LedgerWriter
from spillway.quota import QuotaService
from spillway.redact import REDACTOR
from spillway.routing.filter import (
    Candidate,
    CandidateFilter,
    FilterResult,
    HealthCache,
)
from spillway.routing.outcome import (
    AttemptInfo,
    Plan,
    PlanEntry,
    PlanExcluded,
    PlanSlot,
    RouteOutcome,
)
from spillway.routing.planner import OrderPlanner, Slot, SlotEntry
from spillway.routing.resolver import Resolver, Target
from spillway.routing.selector import SlotSelector

__all__ = [
    "AttemptInfo",
    "InvalidEffort",
    "Plan",
    "PlanEntry",
    "PlanExcluded",
    "PlanSlot",
    "RouteOutcome",
    "Router",
    "normalize_effort",
]

ConnFactory = Callable[[], sqlite3.Connection]

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------- Router


def _resolve_secret(dep: Deployment) -> tuple[str, ...]:
    if not dep.credential_ref:
        return ()
    try:
        return (credentials.resolve(dep.credential_ref),)
    except Exception:  # noqa: BLE001 — 解析失敗:adapter 會自己回 error
        return ()


def _pick_effort(entry_effort: str | None, req_effort: str | None, dep: Deployment) -> str | None:
    caps = (dep.capabilities or {}).get("efforts")
    if caps is not None and not isinstance(caps, (list, tuple)):
        caps = None
    try:
        return EffortMapper.pick(entry_effort, req_effort, caps)
    except ValueError:
        return None


def _elapsed_ms(start: float) -> int:
    return max(0, int((time.perf_counter() - start) * 1000))


def _response_text(result: CallResult) -> str | None:
    if result.tool_calls:
        return json.dumps(
            {"content": result.text, "tool_calls": result.tool_calls,
             "finish_reason": result.finish_reason},
            ensure_ascii=False,
        )
    if result.text:
        return result.text
    return "" if result.status == "ok" else None


class Router:
    def __init__(
        self,
        conn_factory: ConnFactory,
        registry: Any,
        clock: Clock,
        rng: Rng,
        *,
        quota: Any = None,
        estimator: Any = None,
        ledger: LedgerWriter | None = None,
        health_cache: HealthCache | None = None,
        close_conn: bool = False,
    ) -> None:
        self._factory = conn_factory
        self._close_conn = close_conn
        self._current: contextvars.ContextVar[sqlite3.Connection | None] = contextvars.ContextVar(
            f"spillway_router_conn_{id(self)}", default=None
        )
        self.registry = registry
        self.clock = clock
        self.rng = rng
        self.quota = quota if quota is not None else QuotaService(self._conn, clock)
        self.estimator = estimator if estimator is not None else Estimator(self._conn)
        self._ledger = ledger
        self.health_cache = health_cache or HealthCache(clock)
        self.resolver = Resolver(self._conn)
        self.filter = CandidateFilter(registry, self.quota, self.estimator, clock, self.health_cache)
        self.planner = OrderPlanner()
        self.selector = SlotSelector(self.quota, rng)
        self.redactor = REDACTOR

    # -- 連線 -------------------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = self._current.get()
        if conn is None:
            raise RuntimeError("Router 的內建元件只能在 route()/dry_run() 內使用")
        return conn

    def _open(self) -> tuple[sqlite3.Connection, contextvars.Token]:
        conn = self._factory()
        return conn, self._current.set(conn)

    def _close(self, conn: sqlite3.Connection, token: contextvars.Token) -> None:
        self._current.reset(token)
        if self._close_conn:
            conn.close()

    def _ledger_for(self, conn: sqlite3.Connection) -> LedgerWriter:
        return self._ledger if self._ledger is not None else LedgerWriter(conn, self.redactor)

    # -- route ------------------------------------------------------------------------------

    async def route(self, req: ChatRequest) -> RouteOutcome:
        req = dataclasses.replace(req, reasoning_effort=normalize_effort(req.reasoning_effort))
        conn, token = self._open()
        try:
            target = self.resolver.resolve(req.model)
            ledger = self._ledger_for(conn)
            if target.kind == "local_direct":
                return await self._direct(target, req, ledger)
            return await self._routed(target, req, ledger)
        finally:
            self._close(conn, token)

    async def _direct(self, target: Target, req: ChatRequest, ledger: LedgerWriter) -> RouteOutcome:
        dep = target.deployment
        if not dep.enabled:
            return RouteOutcome(None, attempts=(AttemptInfo(dep.id, "disabled", None),))
        try:
            self.registry.module_for(dep)
        except AdapterUnavailableError:
            return RouteOutcome(None, attempts=(AttemptInfo(dep.id, "adapter_unavailable", None),))
        est = self.estimator.estimate(dep, req, None)
        effort = _pick_effort(None, req.reasoning_effort, dep)
        result, info = await self._attempt(
            dep, req, effort, est, ledger, target, slot=None, depth=0)
        if result.status == "ok":
            return RouteOutcome(result, dep.id, None, est, (info,))
        return RouteOutcome(None, attempts=(info,))

    async def _routed(self, target: Target, req: ChatRequest, ledger: LedgerWriter) -> RouteOutcome:
        now = self.clock.now()
        fres = await self.filter.filter(target, req, probe=True, now=now)
        slots, skipped_slot = self._plan(target, fres)
        attempts: list[AttemptInfo] = []
        excluded = [(e.deployment_id, e.reason) for e in fres.excluded
                    if e.reason != STATUS_SKIPPED_PRECHECK]

        for ex in fres.excluded:
            if ex.reason != STATUS_SKIPPED_PRECHECK:
                continue
            slot = skipped_slot.get(ex.deployment_id)
            est = ex.estimate
            ledger.record(Attempt(
                ts=self.clock.now(), status=STATUS_SKIPPED_PRECHECK, route_name=target.route_name,
                deployment_id=ex.deployment_id, slot=slot, fallback_depth=0,
                est_tokens_in=est.tokens_in if est else None,
                est_tokens_out_p50=est.out_p50 if est else None,
                est_tokens_out_p90=est.out_p90 if est else None,
                est_cost_usd_p90=est.cost_usd_p90 if est else None,
                route_record_context=target.record_context,
            ))
            attempts.append(AttemptInfo(ex.deployment_id, STATUS_SKIPPED_PRECHECK, slot))

        depth = 0
        for slot in slots:
            for entry in self.selector.iterate(slot, now):
                dep = entry.deployment
                # S1:篩選之後、輪到它之前,並行請求可能已讓它進入冷卻;重查,冷卻中就略過
                # (不呼叫、不寫帳本、不算 fallback 深度),記入 excluded 為 cooling
                if dep.kind in CLOUD_KINDS and self._cooling_now(dep):
                    excluded.append((dep.id, "cooling"))
                    continue
                effort = _pick_effort(entry.effort, req.reasoning_effort, dep)
                result, info = await self._attempt(
                    dep, req, effort, entry.estimate, ledger, target, slot=slot.number, depth=depth)
                attempts.append(info)
                depth += 1
                if result.status == "ok":
                    return RouteOutcome(result, dep.id, slot.number, entry.estimate,
                                        tuple(attempts), tuple(excluded))
        return RouteOutcome(None, attempts=tuple(attempts), excluded=tuple(excluded))

    def _cooling_now(self, dep: Deployment) -> bool:
        try:
            return bool(self.quota.is_cooling(dep, self.clock.now()))
        except Exception as exc:  # noqa: BLE001 — 重查失敗不擋嘗試(篩選時已確認未冷卻)
            _log.warning("QuotaService.is_cooling 失敗(%s),成員 %s 照常嘗試", type(exc).__name__, dep.id)
            return False

    def _plan(self, target: Target, fres: FilterResult) -> tuple[list[Slot], dict[str, int]]:
        """候選的 slot;另以「假設未被跳過」排一次,取得 skipped_precheck 成員的 slot 編號。"""
        slots = self.planner.plan(target.route_targets, fres.candidates, members=target.members)
        skipped = [e for e in fres.excluded if e.reason == STATUS_SKIPPED_PRECHECK]
        if not skipped:
            return slots, {}
        pseudo = list(fres.candidates) + [
            Candidate(e.deployment, e.estimate or Estimate(0, 0, 0, None)) for e in skipped
        ]
        full = self.planner.plan(target.route_targets, pseudo, members=target.members)
        ids = {e.deployment_id for e in skipped}
        where = {en.deployment.id: s.number for s in full for en in s.entries
                 if en.deployment.id in ids}
        return slots, where

    async def _attempt(
        self,
        dep: Deployment,
        req: ChatRequest,
        effort: str | None,
        est: Estimate | None,
        ledger: LedgerWriter,
        target: Target,
        *,
        slot: int | None,
        depth: int,
    ) -> tuple[CallResult, AttemptInfo]:
        secrets = _resolve_secret(dep)
        ts = self.clock.now()
        start = time.perf_counter()
        try:
            result = await self.registry.get(dep).call(dep, req, effort)
            if not isinstance(result, CallResult):
                raise TypeError(f"adapter 回傳 {type(result).__name__},不是 CallResult")
        except Exception as exc:  # noqa: BLE001 — adapter 例外一律視為該成員失敗,換下一位
            result = CallResult(ok=False, text="", tokens_in=None, tokens_out=None, cost_usd=None,
                                status="error", error=f"{type(exc).__name__}: {exc}")
        except asyncio.CancelledError:
            # 取消:上游可能已收到請求,同步補一列帳本(不跨 await)後重新丟出(S2)
            cancelled = CallResult(ok=False, text="", tokens_in=None, tokens_out=None,
                                   cost_usd=None, status="error", error="cancelled")
            self._record(ledger, dep, req, effort, est, target, cancelled, ts=ts, status="error",
                         slot=slot, depth=depth, latency_ms=_elapsed_ms(start), secrets=secrets)
            raise
        latency_ms = _elapsed_ms(start)
        status = result.status if result.status in CALL_STATUSES else "error"
        if status != result.status:
            result = dataclasses.replace(
                result, ok=False, status="error",
                error=result.error or f"adapter 回傳未知 status {result.status!r}")
        if status in COOLING_STATUSES and dep.kind in CLOUD_KINDS:
            try:
                self.quota.mark(dep, status)
            except Exception as exc:  # noqa: BLE001 — 冷卻設定失敗不可中斷帳本與 fallback(M18)
                _log.warning("QuotaService.mark 失敗(%s),成員 %s 本次未設冷卻",
                             type(exc).__name__, dep.id)
        self._record(ledger, dep, req, effort, est, target, result, ts=ts, status=status,
                     slot=slot, depth=depth, latency_ms=latency_ms, secrets=secrets)
        error = None
        if status != "ok" and result.error is not None:
            error, _ = self.redactor.redact(result.error, secrets)
        return result, AttemptInfo(dep.id, status, slot, error, latency_ms)

    def _record(
        self,
        ledger: LedgerWriter,
        dep: Deployment,
        req: ChatRequest,
        effort: str | None,
        est: Estimate | None,
        target: Target,
        result: CallResult,
        *,
        ts: Any,
        status: str,
        slot: int | None,
        depth: int,
        latency_ms: int,
        secrets: tuple[str, ...],
    ) -> None:
        """寫一次到達 adapter 的嘗試(同步,不跨 await)。"""
        ledger.record(Attempt(
            ts=ts, status=status, route_name=target.route_name, deployment_id=dep.id,
            effort=effort, tokens_in=result.tokens_in, tokens_out=result.tokens_out,
            cost_usd=result.cost_usd, slot=slot, fallback_depth=depth, latency_ms=latency_ms,
            est_tokens_in=est.tokens_in if est else None,
            est_tokens_out_p50=est.out_p50 if est else None,
            est_tokens_out_p90=est.out_p90 if est else None,
            est_cost_usd_p90=est.cost_usd_p90 if est else None,
            error=None if status == "ok" else result.error,
            request_text=json.dumps(req.messages, ensure_ascii=False),
            response_text=_response_text(result),
            extra_secrets=secrets,
            route_record_context=target.record_context,
        ))

    # -- dry_run ----------------------------------------------------------------------------

    async def dry_run(self, route_name_or_model: str, req: ChatRequest) -> Plan:
        req = dataclasses.replace(req, reasoning_effort=normalize_effort(req.reasoning_effort))
        conn, token = self._open()
        try:
            target = self.resolver.resolve(route_name_or_model)
            if target.kind == "local_direct":
                return self._direct_plan(target, req)
            now = self.clock.now()
            fres = await self.filter.filter(target, req, probe=False, now=now)
            slots = self.planner.plan(target.route_targets, fres.candidates, members=target.members)
            plan_slots = tuple(
                PlanSlot(s.number, tuple(self._plan_entry(e, req) for e in self.selector.order(s, now)))
                for s in slots
            )
            first = next((s.entries[0].deployment for s in plan_slots if s.entries), None)
            excluded = tuple(PlanExcluded(e.deployment_id, e.reason, e.estimate) for e in fres.excluded)
            return Plan(target.route_name, plan_slots, excluded, first)
        finally:
            self._close(conn, token)

    @staticmethod
    def _plan_entry(e: SlotEntry, req: ChatRequest) -> PlanEntry:
        return PlanEntry(
            deployment=e.deployment.id, kind=e.kind, weight=e.weight,
            effort=_pick_effort(e.effort, req.reasoning_effort, e.deployment),
            position=e.position, unsorted=e.unsorted, health=e.health, estimate=e.estimate,
        )

    def _direct_plan(self, target: Target, req: ChatRequest) -> Plan:
        dep = target.deployment
        reason = None
        if not dep.enabled:
            reason = "disabled"
        else:
            try:
                self.registry.module_for(dep)
            except AdapterUnavailableError:
                reason = "adapter_unavailable"
        if reason is not None:
            return Plan(None, (), (PlanExcluded(dep.id, reason),), None)
        cached = self.health_cache.get(dep.id)
        health = "health_unknown" if cached is None else ("healthy" if cached else "unhealthy")
        entry = PlanEntry(
            deployment=dep.id, kind=dep.kind, weight="medium",
            effort=_pick_effort(None, req.reasoning_effort, dep), position=0, unsorted=False,
            health=health, estimate=self.estimator.estimate(dep, req, None),
        )
        return Plan(None, (PlanSlot(None, (entry,)),), (), dep.id)

"""Router:單次請求的完整路由(spec 4.5、5.4.1、5.4.5、6.1、6.2;CHG-20261030-02 T12;決策 18、19、20、29)。

`Router(conn_factory, registry, clock, rng, *, quota=None, estimator=None, ledger=None,
health_cache=None, close_conn=False)`

- `await route(req) -> RouteOutcome`:解析 → 篩選(probe=True)→ 排序 → 逐 slot 選擇並呼叫。
- `await dry_run(route_name_or_model, req) -> Plan`:同一管線到 SlotSelector 為止(probe=False);
  不呼叫 adapter、不寫帳本、不設冷卻。
- `normalize_effort(value)`:請求 `reasoning_effort` 正規化(決策 29)。

連線(design D2「每請求一連線」;施工審議波次 3 建議修):
    `conn_factory` 是零參數函式,回傳 `sqlite3.Connection`。每次 `route()` / `dry_run()` 只呼叫
    一次,這個連線供本次請求的 Resolver、QuotaService、Estimator、LedgerWriter 共用(以
    `contextvars` 保存,並行請求各用各的)。`close_conn=True` 時請求結束後關閉該連線(工廠每次
    開新連線的情況);預設 False(工廠回傳共用連線,例如測試的 `lambda: db`)。
    `quota`、`estimator`、`ledger` 可注入;未給時以「本次請求的連線」建立。`ledger` 未給時每次
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
6. 全部失敗 → `RouteOutcome(result=None, attempts=...)`;每筆 `AttemptInfo.error` 以該次的
   `extra_secrets` 經 Redactor 遮罩(帳本由 LedgerWriter 自行遮罩)。`excluded` 另列被篩掉的成員與原因。
"""
from __future__ import annotations

import contextvars
import dataclasses
import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from spillway import credentials
from spillway.adapters.base import AdapterUnavailableError
from spillway.clock import Clock, Rng
from spillway.domain import CALL_STATUSES, EFFORTS, CallResult, ChatRequest, Deployment
from spillway.effort import EffortMapper
from spillway.estimate import Estimate, Estimator
from spillway.ledger import Attempt, LedgerWriter
from spillway.quota import QuotaService
from spillway.redact import Redactor
from spillway.routing.filter import (
    CLOUD_KINDS,
    Candidate,
    CandidateFilter,
    FilterResult,
    HealthCache,
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

_EFFORT_ALIASES = {"minimal": "none"}


class InvalidEffort(ValueError):
    """請求的 `reasoning_effort` 不在五級內(決策 29;T13 轉 422 `validation_error`)。

    訊息不回顯原值以外的內容;原值放在 `value`。
    """

    def __init__(self, value: object) -> None:
        super().__init__(
            f"reasoning_effort 不合法:{value!r},只可為 {', '.join(EFFORTS)}(或 minimal)"
        )
        self.value = value


def normalize_effort(value: object) -> str | None:
    """決策 29:None 或空白字串 → None;不分大小寫;`minimal` → `none`;其餘不在五級內丟 `InvalidEffort`。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidEffort(value)
    v = value.strip().lower()
    if not v:
        return None
    v = _EFFORT_ALIASES.get(v, v)
    if v not in EFFORTS:
        raise InvalidEffort(value)
    return v


# ---------------------------------------------------------------- 回傳型別


@dataclass(frozen=True)
class AttemptInfo:
    """一次嘗試的摘要(503 body、MCP `ask`)。

    `status`:帳本 status 五值之一;直呼無法呼叫時為 `disabled` / `adapter_unavailable`(不寫帳本)。
    `error`:已遮罩。`latency_ms`:沒有呼叫 adapter 時為 None。
    """

    deployment: str
    status: str
    slot: int | None
    error: str | None = None
    latency_ms: int | None = None


@dataclass(frozen=True)
class RouteOutcome:
    """`route()` 的結果。`result` 為 None 即 `no_candidate`(T13 回 503)。

    `served_by`、`slot`、`estimate` 為成功那次的成員 id、slot 編號(直呼為 None)、呼叫前預估。
    `attempts` 依發生順序;`excluded` 為被篩掉的成員 `(deployment_id, reason)`(不含
    `skipped_precheck`,它已在 attempts 中)。
    """

    result: CallResult | None
    served_by: str | None = None
    slot: int | None = None
    estimate: Estimate | None = None
    attempts: tuple[AttemptInfo, ...] = ()
    excluded: tuple[tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return self.result is not None


@dataclass(frozen=True)
class PlanEntry:
    deployment: str
    kind: str
    weight: str
    effort: str | None           # EffortMapper 降級後、實際會送出的等級
    position: int
    unsorted: bool
    health: str | None           # cli/api 為 None;local 為 healthy / health_unknown
    estimate: Estimate | None


@dataclass(frozen=True)
class PlanSlot:
    number: int | None           # `local/` 直呼為 None
    entries: tuple[PlanEntry, ...]   # 依注入 Rng 以 SlotSelector.order 抽出的順序


@dataclass(frozen=True)
class PlanExcluded:
    deployment: str
    reason: str
    estimate: Estimate | None = None   # 只有 skipped_precheck 有


@dataclass(frozen=True)
class Plan:
    route_name: str | None
    slots: tuple[PlanSlot, ...]
    excluded: tuple[PlanExcluded, ...]
    first: str | None            # 本次抽出的第一順位;沒有候選時為 None

    @property
    def unsorted(self) -> tuple[str, ...]:
        return tuple(e.deployment for s in self.slots for e in s.entries if e.unsorted)


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
        self.redactor = Redactor()

    # -- 連線 -------------------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = self._current.get()
        return conn if conn is not None else self._factory()

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
        excluded = tuple((e.deployment_id, e.reason) for e in fres.excluded
                         if e.reason != "skipped_precheck")

        for ex in fres.excluded:
            if ex.reason != "skipped_precheck":
                continue
            slot = skipped_slot.get(ex.deployment_id)
            est = ex.estimate
            ledger.record(Attempt(
                ts=self.clock.now(), status="skipped_precheck", route_name=target.route_name,
                deployment_id=ex.deployment_id, slot=slot, fallback_depth=0,
                est_tokens_in=est.tokens_in if est else None,
                est_tokens_out_p50=est.out_p50 if est else None,
                est_tokens_out_p90=est.out_p90 if est else None,
                est_cost_usd_p90=est.cost_usd_p90 if est else None,
                route_record_context=target.record_context,
            ))
            attempts.append(AttemptInfo(ex.deployment_id, "skipped_precheck", slot))

        depth = 0
        for slot in slots:
            for entry in self.selector.iterate(slot, now):
                dep = entry.deployment
                effort = _pick_effort(entry.effort, req.reasoning_effort, dep)
                result, info = await self._attempt(
                    dep, req, effort, entry.estimate, ledger, target, slot=slot.number, depth=depth)
                attempts.append(info)
                depth += 1
                if result.status == "ok":
                    return RouteOutcome(result, dep.id, slot.number, entry.estimate,
                                        tuple(attempts), excluded)
        return RouteOutcome(None, attempts=tuple(attempts), excluded=excluded)

    def _plan(self, target: Target, fres: FilterResult) -> tuple[list[Slot], dict[str, int]]:
        """候選的 slot;另以「假設未被跳過」排一次,取得 skipped_precheck 成員的 slot 編號。"""
        slots = self.planner.plan(target.route_targets, fres.candidates, members=target.members)
        skipped = [e for e in fres.excluded if e.reason == "skipped_precheck"]
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
        latency_ms = max(0, int((time.perf_counter() - start) * 1000))
        status = result.status if result.status in CALL_STATUSES else "error"
        if status != result.status:
            result = dataclasses.replace(
                result, ok=False, status="error",
                error=result.error or f"adapter 回傳未知 status {result.status!r}")
        if status in ("quota_exhausted", "rate_limited") and dep.kind in CLOUD_KINDS:
            self.quota.mark(dep, status)
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
        error = None
        if status != "ok" and result.error is not None:
            error, _ = self.redactor.redact(result.error, secrets)
        return result, AttemptInfo(dep.id, status, slot, error, latency_ms)

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

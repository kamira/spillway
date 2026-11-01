"""Router 的回傳型別(CHG-20261101-02 T5;決策 10;原定義於 `routing/router.py`)。

- `RouteOutcome`、`AttemptInfo`:`Router.route()` 的結果與每次嘗試的摘要。
- `Plan`、`PlanSlot`、`PlanEntry`、`PlanExcluded`:`Router.dry_run()` 的結果。

`spillway.routing` 與 `spillway.routing.router` 轉匯出同一物件;欄位不變。
"""
from __future__ import annotations

from dataclasses import dataclass

from spillway.domain import CallResult
from spillway.estimate import Estimate

__all__ = [
    "AttemptInfo",
    "Plan",
    "PlanEntry",
    "PlanExcluded",
    "PlanSlot",
    "RouteOutcome",
]


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

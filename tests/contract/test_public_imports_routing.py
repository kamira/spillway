"""routing 公開匯入路徑保留(CHG-20261101-02 T5;決策 10;Global Constraints「公開匯入路徑保留」)。

`routing/router.py` 的回傳 dataclass 移到 `routing/outcome.py`;`spillway.routing` 與
`spillway.routing.router` 的 `__all__` 與拆檔前相同,轉匯出必須是同一物件,欄位名稱不變。
"""
from __future__ import annotations

import dataclasses

import spillway.domain as domain
import spillway.effort as effort
import spillway.routing as routing
import spillway.routing.outcome as outcome
import spillway.routing.router as router

# 拆檔前(d744265)以 `dataclasses.fields` 取得的名稱清單
FIELDS_BEFORE_SPLIT = {
    "AttemptInfo": ["deployment", "status", "slot", "error", "latency_ms"],
    "RouteOutcome": ["result", "served_by", "slot", "estimate", "attempts", "excluded"],
    "PlanEntry": [
        "deployment", "kind", "weight", "effort", "position", "unsorted", "health", "estimate",
    ],
    "PlanSlot": ["number", "entries"],
    "PlanExcluded": ["deployment", "reason", "estimate"],
    "Plan": ["route_name", "slots", "excluded", "first"],
}

# 拆檔前的快照(刻意寫死;日後增減匯出須有意識地同步修改)
ROUTER_ALL_BEFORE_SPLIT = [
    "AttemptInfo", "InvalidEffort", "Plan", "PlanEntry", "PlanExcluded", "PlanSlot",
    "RouteOutcome", "Router", "normalize_effort",
]

# 拆檔前的快照(刻意寫死;日後增減匯出須有意識地同步修改)
ROUTING_ALL_BEFORE_SPLIT = [
    "members_of", "matches", "selector_of",
    "Resolver", "RouteNotFound", "Target", "LOCAL_PREFIX", "ROUTE_PREFIX", "TARGET_KINDS",
    "CandidateFilter", "Candidate", "Excluded", "FilterResult", "HealthCache", "HEALTH_TTL",
    "HEALTH_PROBE_TIMEOUT", "HEALTHY", "HEALTH_UNKNOWN", "EXCLUDE_REASONS", "CLOUD_KINDS",
    "OrderPlanner", "Slot", "SlotEntry", "DEFAULT_WEIGHT",
    "SlotSelector", "WEIGHT_VALUES",
    "Router", "RouteOutcome", "AttemptInfo", "Plan", "PlanSlot", "PlanEntry", "PlanExcluded",
    "InvalidEffort", "normalize_effort",
]


def test_l1_imports_still_work():
    from spillway.routing import AttemptInfo, Plan, PlanExcluded, RouteOutcome  # noqa: F401
    from spillway.routing.router import (  # noqa: F401
        InvalidEffort,
        Plan,
        RouteOutcome,
        normalize_effort,
    )


def test_outcome_module_defines_all_return_dataclasses():
    assert sorted(outcome.__all__) == sorted(FIELDS_BEFORE_SPLIT)
    for name in FIELDS_BEFORE_SPLIT:
        cls = getattr(outcome, name)
        assert dataclasses.is_dataclass(cls)
        assert cls.__module__ == "spillway.routing.outcome"


def test_dataclass_field_names_unchanged():
    for name, fields in FIELDS_BEFORE_SPLIT.items():
        assert [f.name for f in dataclasses.fields(getattr(outcome, name))] == fields


def test_reexports_are_same_object():
    for name in FIELDS_BEFORE_SPLIT:
        assert getattr(router, name) is getattr(outcome, name)
        assert getattr(routing, name) is getattr(outcome, name)


def test_router_all_unchanged():
    assert router.__all__ == ROUTER_ALL_BEFORE_SPLIT
    for name in router.__all__:
        assert getattr(routing, name) is getattr(router, name)


def test_routing_all_unchanged():
    assert routing.__all__ == ROUTING_ALL_BEFORE_SPLIT
    for name in routing.__all__:
        assert hasattr(routing, name)


def test_routing_cloud_kinds_and_effort_are_same_object():
    assert routing.CLOUD_KINDS is domain.CLOUD_KINDS
    assert routing.normalize_effort is effort.normalize_effort
    assert routing.InvalidEffort is effort.InvalidEffort

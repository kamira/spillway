"""路由(spec 4;CHG-20261030-02 T11 擁有,T12 加入 `router.py`)。

管線:`Resolver`(4.1)→ `CandidateFilter`(4.2)→ `OrderPlanner`(4.3)→ `SlotSelector`(4.4)。
各元件可單獨測試,也對應 dry-run `Plan` 的中間結果。
"""
from spillway.routing.filter import (
    CLOUD_KINDS,
    EXCLUDE_REASONS,
    HEALTH_PROBE_TIMEOUT,
    HEALTH_TTL,
    HEALTH_UNKNOWN,
    HEALTHY,
    Candidate,
    CandidateFilter,
    Excluded,
    FilterResult,
    HealthCache,
)
from spillway.routing.planner import DEFAULT_WEIGHT, OrderPlanner, Slot, SlotEntry
from spillway.routing.resolver import (
    LOCAL_PREFIX,
    ROUTE_PREFIX,
    TARGET_KINDS,
    Resolver,
    RouteNotFound,
    Target,
)
from spillway.routing.selector import WEIGHT_VALUES, SlotSelector
from spillway.routing.target import matches, members_of, selector_of

__all__ = [
    "members_of", "matches", "selector_of",
    "Resolver", "RouteNotFound", "Target", "LOCAL_PREFIX", "ROUTE_PREFIX", "TARGET_KINDS",
    "CandidateFilter", "Candidate", "Excluded", "FilterResult", "HealthCache", "HEALTH_TTL",
    "HEALTH_PROBE_TIMEOUT",    "HEALTHY", "HEALTH_UNKNOWN", "EXCLUDE_REASONS", "CLOUD_KINDS",
    "OrderPlanner", "Slot", "SlotEntry", "DEFAULT_WEIGHT",
    "SlotSelector", "WEIGHT_VALUES",
]

# T12(CHG-20261030-02):Router 與其回傳型別(只追加匯出)
from spillway.routing.router import (  # noqa: E402
    AttemptInfo,
    InvalidEffort,
    Plan,
    PlanEntry,
    PlanExcluded,
    PlanSlot,
    RouteOutcome,
    Router,
    normalize_effort,
)

__all__ += [
    "Router", "RouteOutcome", "AttemptInfo", "Plan", "PlanSlot", "PlanEntry", "PlanExcluded",
    "InvalidEffort", "normalize_effort",
]

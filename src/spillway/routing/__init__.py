"""路由(spec 4;CHG-20261030-02 T11 擁有,T12 加入 `router.py`)。

管線:`Resolver`(4.1)→ `CandidateFilter`(4.2)→ `OrderPlanner`(4.3)→ `SlotSelector`(4.4)。
各元件可單獨測試,也對應 dry-run `Plan` 的中間結果。
`CLOUD_KINDS` 定義在 `spillway.domain`,此處轉匯出(CHG-20261101-02 決策 6)。
"""
from spillway.domain import CLOUD_KINDS
from spillway.routing.filter import (
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
    "HEALTH_PROBE_TIMEOUT", "HEALTHY", "HEALTH_UNKNOWN", "EXCLUDE_REASONS", "CLOUD_KINDS",
    "OrderPlanner", "Slot", "SlotEntry", "DEFAULT_WEIGHT",
    "SlotSelector", "WEIGHT_VALUES",
]

# T12(CHG-20261030-02):Router 與其回傳型別(只追加匯出)
# 回傳型別定義在 `routing.outcome`(CHG-20261101-02 決策 10);effort 定義在 `spillway.effort`(決策 8)
from spillway.effort import InvalidEffort, normalize_effort  # noqa: E402
from spillway.routing.outcome import (  # noqa: E402
    AttemptInfo,
    Plan,
    PlanEntry,
    PlanExcluded,
    PlanSlot,
    RouteOutcome,
)
from spillway.routing.router import Router  # noqa: E402

__all__ += [
    "Router", "RouteOutcome", "AttemptInfo", "Plan", "PlanSlot", "PlanEntry", "PlanExcluded",
    "InvalidEffort", "normalize_effort",
]

# CHG-20261103-01 決策 10 ②:靜態順序計畫(`/api/routes/{name}/effective-order` 與 MCP `list_routes` 共用)。
# 以 `spillway.routing.static_plan` 取用;`__all__` 由 tests/contract/test_public_imports_routing.py 寫死,
# 不在本 CHG 允許清單內,故不加入 `__all__`。
from spillway.routing.static_plan import static_plan  # noqa: E402,F401

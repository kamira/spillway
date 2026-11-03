"""靜態順序計畫(CHG-20261103-01 決策 10 ②;決策審議 AM-1)。

`static_plan(route, targets, deployments, groups) -> list[Slot]`

`GET /api/routes/{name}/effective-order` 與 MCP `list_routes` 共用的純函式(不查詢、不讀健康與冷卻):

- 候選 = `members_of(route.target, deployments, groups)` 的**全部**成員,含停用、冷卻、健康不佳者,
  不經 `CandidateFilter`;只有 chat route 排除 `cli` 成員(與 `CandidateFilter` 的 mode 規則相同)。
- `OrderPlanner().plan(targets, candidates, members)`:手動 slot 保留原編號(可能不連號),不在
  `targets` 的成員依 4.3.2 接在後面並帶 `unsorted=True`;已不是成員的 target(stale)不輸出。
- 群組不存在或沒有成員 → `[]`。

`targets` 為該 route 的 `repo.list_route_targets()`(已依 `(slot, position, id)` 排序);
`groups` 必填(同 `members_of` 的 `groups`:`repo.list_groups()` 或 `{name: selector}`);
省略會讓 `{group}` 目標一律視為群組不存在,故不給預設值。
"""
from __future__ import annotations

from collections.abc import Sequence

from spillway.domain import Deployment
from spillway.routing.filter import Candidate
from spillway.routing.planner import OrderPlanner, Slot
from spillway.routing.target import GroupsArg, members_of
from spillway.store.repo import Route, RouteTarget


def static_plan(
    route: Route,
    targets: Sequence[RouteTarget],
    deployments: Sequence[Deployment],
    groups: GroupsArg,
) -> list[Slot]:
    members = members_of(route.target, deployments, groups)
    pool = [d for d in members if not (route.mode == "chat" and d.kind == "cli")]
    cands = [Candidate(deployment=d, estimate=None) for d in pool]  # type: ignore[arg-type]
    return OrderPlanner().plan(targets, cands, members)

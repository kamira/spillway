"""順序決定(spec 4.3.1–4.3.4;CHG-20261030-02 T11)。

`OrderPlanner().plan(route_targets, candidates, members=None) -> list[Slot]`

- `route_targets`:該 route 的手動順序(`repo.list_route_targets()`;空 = 無手動排序)。
- `candidates`:`CandidateFilter` 的候選;**只有候選會出現在 slot 中**,空 slot 移除。
- `members`:目標的全部成員(`Target.members`,含被篩掉者),只用來決定預設層級的 slot 編號,
  讓編號不因本次哪些成員被篩掉而變動(`X-Spillway-Slot`、dry-run 穩定)。省略時以候選代替。

規則:
1. 有手動順序(4.3.1 ①):出現在 `route_targets` 的候選依其 `slot`(保留原編號,小者先)、
   `position`、id 排列,帶入 route_targets 的 `weight`、`effort`、`position`,`unsorted=False`。
2. 不在 `route_targets` 的成員(4.3.3)依 4.3.2 層級接在手動 slot 之後,`unsorted=True`;
   編號從「全部 route_targets 的最大 slot + 1」起算。
3. 無手動順序(4.3.1 ②、4.3.2):層級為「全部 cli 一個 slot → 全部 api 一個 slot →
   每個 local 各一個 slot(依 id 字母序)」,`unsorted=False`。
   層級編號固定(spec 4.3.2 字面;施工審議 M16、代決 D-7):cli 層為 `base`、api 層為 `base + 1`、
   第 i 個 local(`members` 中 local 依 id 字母序,i 從 0 起)為 `base + 2 + i`。無手動排序時
   `base = 1`;有手動排序時 `base` 為規則 2 的起點。某層沒有成員或成員全被篩掉時該 slot 不輸出,
   其他層的編號不重排(例如沒有 cli 成員時 api 仍為 slot 2)。
   層級內 `position` 依 id 字母序從 0 起算;`weight="medium"`、`effort=None`。

`SlotEntry` 帶 deployment、weight、effort、position、unsorted,以及候選的 estimate、health,
供 SlotSelector、Router 寫帳本與 dry-run 的 `Plan` 使用。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from spillway.domain import Deployment
from spillway.estimate import Estimate
from spillway.routing.filter import Candidate
from spillway.store.repo import RouteTarget

DEFAULT_WEIGHT = "medium"


@dataclass(frozen=True)
class SlotEntry:
    deployment: Deployment
    weight: str                  # high | medium | low
    effort: str | None           # route_targets 的 effort;預設層級為 None
    position: int
    unsorted: bool
    estimate: Estimate | None = None
    health: str | None = None    # 同 Candidate.health

    @property
    def kind(self) -> str:
        return self.deployment.kind


@dataclass(frozen=True)
class Slot:
    number: int                  # slot 編號(帳本 `slot` 欄、`X-Spillway-Slot`)
    entries: tuple[SlotEntry, ...]


def _tiers(deps: Iterable[Deployment]) -> list[list[Deployment]]:
    """4.3.2 層級:索引 0 為 cli、1 為 api、2 起每個 local 一層(依 id)。空層保留(編號固定)。"""
    by_id = sorted(deps, key=lambda d: d.id)
    tiers = [[d for d in by_id if d.kind == "cli"], [d for d in by_id if d.kind == "api"]]
    tiers += [[d] for d in by_id if d.kind == "local"]
    return tiers


class OrderPlanner:
    def plan(
        self,
        route_targets: Sequence[RouteTarget],
        candidates: Sequence[Candidate],
        members: Sequence[Deployment] | None = None,
    ) -> list[Slot]:
        by_id = {c.deployment.id: c for c in candidates}
        manual = {rt.deployment_id: rt for rt in route_targets}
        slots: list[Slot] = []

        # 1. 手動 slot
        if manual:
            numbers = sorted({rt.slot for rt in route_targets})
            for n in numbers:
                rts = sorted((rt for rt in route_targets if rt.slot == n),
                             key=lambda rt: (rt.position, rt.deployment_id))
                entries = tuple(
                    self._entry(by_id[rt.deployment_id], rt.weight, rt.effort, rt.position, False)
                    for rt in rts if rt.deployment_id in by_id
                )
                if entries:
                    slots.append(Slot(n, entries))

        # 2./3. 預設層級(無手動時為全部;有手動時只含未出現在 route_targets 者)
        pool = list(members) if members is not None else []
        seen = {d.id for d in pool}
        pool += [c.deployment for c in candidates if c.deployment.id not in seen]
        rest = [d for d in pool if d.id not in manual]
        start = (max(rt.slot for rt in route_targets) + 1) if manual else 1
        for i, tier in enumerate(_tiers(rest)):
            entries = tuple(
                self._entry(by_id[d.id], DEFAULT_WEIGHT, None, pos, bool(manual))
                for pos, d in enumerate(tier) if d.id in by_id
            )
            if entries:
                slots.append(Slot(start + i, entries))
        return slots

    @staticmethod
    def _entry(c: Candidate, weight: str, effort: str | None, position: int,
               unsorted: bool) -> SlotEntry:
        return SlotEntry(deployment=c.deployment, weight=weight, effort=effort,
                         position=position, unsorted=unsorted, estimate=c.estimate,
                         health=c.health)

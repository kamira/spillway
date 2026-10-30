"""Slot 內選擇(spec 4.4.1–4.4.5;CHG-20261030-02 T11;決策 6)。

`SlotSelector(quota, rng).iterate(slot, now) -> Iterator[SlotEntry]`

產生器;呼叫端取得一個成員去呼叫,**失敗才 `next()` 取下一個**(成功就停止迭代),
因此每次抽選都在「尚未嘗試的剩餘成員」中重新加權(4.4.3)。

1. 雲端成員(cli、api)先(4.4.1):權重 = `WEIGHT_VALUES[weight]` × `quota.ratio(dep, now)`
   (比例 0–1;無上限 / `none` 為 1,決策 6)。比例在 `iterate()` 開始時對每個雲端成員各讀一次。
   每次抽選:`r = rng.random() × 總權重`,依 (position, id) 順序累加權重,取第一個累計值 > r
   的成員(權重 0 的成員不會被抽中);抽出後移出剩餘清單。
   剩餘成員權重全為 0(例如比例皆為 0)時,依 (position, id) 順序取第一個,不再消耗亂數。
2. 雲端用盡後依 (position, id) 依序產出 local 成員(4.4.4)。
3. 全部產出完即結束,呼叫端進入下一個 slot(4.4.5)。

`order(slot, now)` 一次取完整順序(dry-run 用);`first(slot, now)` 只抽第一順位。
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Protocol

from spillway.clock import Rng
from spillway.domain import Deployment
from spillway.routing.planner import Slot, SlotEntry

WEIGHT_VALUES = {"high": 3, "medium": 2, "low": 1}
CLOUD_KINDS = ("cli", "api")


class _Quota(Protocol):
    def ratio(self, dep: Deployment, now: datetime | None = None) -> float: ...


def _pos_key(e: SlotEntry) -> tuple[int, str]:
    return (e.position, e.deployment.id)


class SlotSelector:
    def __init__(self, quota: _Quota, rng: Rng) -> None:
        self._quota = quota
        self._rng = rng

    def weight_of(self, entry: SlotEntry, now: datetime) -> float:
        ratio = min(1.0, max(0.0, float(self._quota.ratio(entry.deployment, now))))
        return WEIGHT_VALUES.get(entry.weight, WEIGHT_VALUES["medium"]) * ratio

    def iterate(self, slot: Slot, now: datetime) -> Iterator[SlotEntry]:
        cloud = sorted((e for e in slot.entries if e.kind in CLOUD_KINDS), key=_pos_key)
        local = sorted((e for e in slot.entries if e.kind not in CLOUD_KINDS), key=_pos_key)
        pool = [(e, self.weight_of(e, now)) for e in cloud]
        while pool:
            idx = self._draw([w for _, w in pool])
            entry, _ = pool.pop(idx)
            yield entry
        yield from local

    def _draw(self, weights: list[float]) -> int:
        total = sum(weights)
        if total <= 0:
            return 0
        r = self._rng.random() * total
        acc = 0.0
        last_positive = 0
        for i, w in enumerate(weights):
            if w <= 0:
                continue
            acc += w
            last_positive = i
            if r < acc:
                return i
        return last_positive  # 浮點誤差:r 恰等於總和時取最後一個正權重者

    def order(self, slot: Slot, now: datetime) -> list[SlotEntry]:
        return list(self.iterate(slot, now))

    def first(self, slot: Slot, now: datetime) -> SlotEntry | None:
        return next(self.iterate(slot, now), None)

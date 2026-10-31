"""可注入的時間與亂數來源(design D4)。

業務程式一律經這裡取時間與亂數;本檔是唯一允許使用 `datetime.now()` 與 `random` 的地方。
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Iterable, Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc).replace(microsecond=0)


class FakeClock:
    """測試用時鐘:固定在給定時間,只有 `advance()` 會前進。"""

    def __init__(self, start: datetime | None = None) -> None:
        start = start or datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
        if start.tzinfo is None:
            raise ValueError("FakeClock 需要帶時區的時間")
        self._now = start.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta: float) -> datetime:
        self._now += timedelta(**delta)
        return self._now

    def set(self, when: datetime) -> None:
        self._now = _require_aware(when).astimezone(timezone.utc)


class Rng(Protocol):
    def random(self) -> float: ...


class SystemRng:
    def __init__(self, seed: int | None = None) -> None:
        self._r = random.Random(seed)

    def random(self) -> float:
        return self._r.random()


class SeqRng:
    """依給定序列回傳值,用完後循環;讓加權抽選的結果可預測。"""

    def __init__(self, values: Iterable[float]) -> None:
        self._values = list(values)
        if not self._values:
            raise ValueError("SeqRng 需要至少一個值")
        self._i = 0

    def random(self) -> float:
        v = self._values[self._i % len(self._values)]
        self._i += 1
        return v


def _require_aware(dt: datetime) -> datetime:
    # naive 時間會被 astimezone 當成本機時區靜默換算,一律拒絕
    if dt.tzinfo is None:
        raise ValueError("需要帶時區的時間")
    return dt


def to_iso(dt: datetime) -> str:
    """UTC ISO 8601,精度到秒(spec 2.1.2)。"""
    return _require_aware(dt).astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

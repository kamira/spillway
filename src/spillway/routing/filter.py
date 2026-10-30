"""候選篩選(spec 4.2.1–4.2.6;CHG-20261030-02 T11;決策 6、14、20)。

`CandidateFilter(registry, quota, estimator, clock, health_cache=None)`
`await filter(target, req, *, probe=True, now=None) -> FilterResult`

對 `target.members`(依其順序)逐一檢查,第一個不符合的條件即為該成員的排除原因:

| 順序 | 原因 | 條件 |
|---|---|---|
| 1 | `disabled` | `enabled` 為 False(4.2.1) |
| 2 | `mode_chat_excludes_cli` | 有效 mode 為 `chat` 且 kind = cli;`req.metadata["mode"] == "task"` 時有效 mode 為 task(4.2.3) |
| 3 | `ctx_exceeded` | `ceil(req.text_chars / 4) > capabilities.ctx`;沒有 ctx(或非數值)時不限制(4.2.2) |
| 4 | `tools_unsupported` | 請求含 tools 且 `capabilities.tools` 不為 True(缺值視為不支援) |
| 5 | `vision_unsupported` | 請求含圖片且 `capabilities.vision` 不為 True(缺值視為不支援) |
| 6 | `adapter_unavailable` | `registry.module_for(dep)` 丟 `AdapterUnavailableError`(含 `unavailable()` 中的 provider,決策 14;spec 7.6.7) |
| 7 | `cooling` | cli/api:`quota.is_cooling(dep, now)`(4.2.4、4.2.6) |
| 8 | `quota_exhausted` | cli/api:`quota.remaining(dep, now) <= 0`(無上限視為無限,決策 6) |
| 9 | `unhealthy` | local:健康檢查為 False(含 health 丟例外) |
| 10 | `skipped_precheck` | cli/api 且 `target.budget_precheck` 且 `estimate.cost_usd_p90` 不為 None 且 > 剩餘(4.2.5、5.4.4) |

預估:通過 1–9 的成員以 `estimator.estimate(dep, req, target.route_name)` 計算一次,附在
`Candidate.estimate`(T12 寫帳本與 header 用,不必重算);被 precheck 跳過者附在
`Excluded.estimate`(T12 依 4.2.5 為它寫一列 `skipped_precheck`)。其他被篩成員的 estimate 為 None。

local 健康(決策 20):結果以 `HealthCache` 快取 `HEALTH_TTL`(10 秒,時間經 Clock)。
- 快取有效 → 用快取值。
- 冷快取且 `probe=True`(`route()`)→ `await registry.get(dep).health(dep)` 一次並寫入快取。
- 冷快取且 `probe=False`(`dry_run()`)→ 不探測、不寫快取,`Candidate.health = "health_unknown"`,照樣列為候選。
`Candidate.health`:cli/api 為 None;local 為 `"healthy"` 或 `"health_unknown"`。

`target.kind == "local_direct"` 也可傳入(會照上表檢查),但 4.1.1 的直呼不經篩選,Router 不需要呼叫。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from spillway.adapters.base import AdapterUnavailableError
from spillway.clock import Clock
from spillway.domain import ChatRequest, Deployment
from spillway.estimate import Estimate
from spillway.routing.resolver import Target

HEALTH_TTL = timedelta(seconds=10)
CLOUD_KINDS = ("cli", "api")

HEALTHY = "healthy"
HEALTH_UNKNOWN = "health_unknown"

EXCLUDE_REASONS = (
    "disabled",
    "mode_chat_excludes_cli",
    "ctx_exceeded",
    "tools_unsupported",
    "vision_unsupported",
    "adapter_unavailable",
    "cooling",
    "quota_exhausted",
    "unhealthy",
    "skipped_precheck",
)


class _Registry(Protocol):
    def module_for(self, dep: Deployment) -> Any: ...
    def get(self, dep: Deployment) -> Any: ...


class _Quota(Protocol):
    def remaining(self, dep: Deployment, now: datetime | None = None) -> float: ...
    def is_cooling(self, dep: Deployment, now: datetime | None = None) -> bool: ...


class _Estimator(Protocol):
    def estimate(self, dep: Deployment, req: ChatRequest, route_name: str | None) -> Estimate: ...


@dataclass(frozen=True)
class Candidate:
    """通過篩選的成員與其預估;`health` 見模組說明。"""

    deployment: Deployment
    estimate: Estimate
    health: str | None = None


@dataclass(frozen=True)
class Excluded:
    """被篩掉的成員:`reason` 為 `EXCLUDE_REASONS` 之一;`estimate` 只有 `skipped_precheck` 才有。"""

    deployment_id: str
    reason: str
    deployment: Deployment
    estimate: Estimate | None = None


@dataclass(frozen=True)
class FilterResult:
    candidates: tuple[Candidate, ...]
    excluded: tuple[Excluded, ...]

    def reasons(self) -> dict[str, str]:
        """deployment_id → 排除原因。"""
        return {e.deployment_id: e.reason for e in self.excluded}


class HealthCache:
    """local 健康結果快取(決策 20);過期時間以注入的 Clock 判斷。"""

    def __init__(self, clock: Clock, ttl: timedelta = HEALTH_TTL) -> None:
        self._clock = clock
        self._ttl = ttl
        self._data: dict[str, tuple[datetime, bool]] = {}

    def get(self, dep_id: str) -> bool | None:
        hit = self._data.get(dep_id)
        if hit is None:
            return None
        at, healthy = hit
        if self._clock.now() - at >= self._ttl:
            return None
        return healthy

    def put(self, dep_id: str, healthy: bool) -> None:
        self._data[dep_id] = (self._clock.now(), healthy)

    def clear(self) -> None:
        self._data.clear()


def _effective_mode(target: Target, req: ChatRequest) -> str:
    if (req.metadata or {}).get("mode") == "task":
        return "task"
    return target.mode


def _ctx_exceeded(dep: Deployment, req: ChatRequest) -> bool:
    ctx = (dep.capabilities or {}).get("ctx")
    if not isinstance(ctx, (int, float)) or isinstance(ctx, bool):
        return False
    return math.ceil(req.text_chars / 4) > ctx


class CandidateFilter:
    def __init__(
        self,
        registry: _Registry,
        quota: _Quota,
        estimator: _Estimator,
        clock: Clock,
        health_cache: HealthCache | None = None,
    ) -> None:
        self._registry = registry
        self._quota = quota
        self._estimator = estimator
        self._clock = clock
        self.health_cache = health_cache or HealthCache(clock)

    async def filter(
        self,
        target: Target,
        req: ChatRequest,
        *,
        probe: bool = True,
        now: datetime | None = None,
    ) -> FilterResult:
        now = now or self._clock.now()
        mode = _effective_mode(target, req)
        candidates: list[Candidate] = []
        excluded: list[Excluded] = []

        for dep in target.members:
            reason = self._static_reason(dep, req, mode)
            health: str | None = None
            remaining = math.inf
            if reason is None and dep.kind in CLOUD_KINDS:
                if self._quota.is_cooling(dep, now):
                    reason = "cooling"
                else:
                    remaining = self._quota.remaining(dep, now)
                    if remaining <= 0:
                        reason = "quota_exhausted"
            elif reason is None and dep.kind == "local":
                health = await self._health(dep, probe)
                if health is None:
                    reason = "unhealthy"
            if reason is not None:
                excluded.append(Excluded(dep.id, reason, dep))
                continue

            est = self._estimator.estimate(dep, req, target.route_name)
            if (
                target.budget_precheck
                and dep.kind in CLOUD_KINDS
                and est.cost_usd_p90 is not None
                and est.cost_usd_p90 > remaining
            ):
                excluded.append(Excluded(dep.id, "skipped_precheck", dep, est))
                continue
            candidates.append(Candidate(dep, est, health))

        return FilterResult(tuple(candidates), tuple(excluded))

    def _static_reason(self, dep: Deployment, req: ChatRequest, mode: str) -> str | None:
        caps = dep.capabilities or {}
        if not dep.enabled:
            return "disabled"
        if mode == "chat" and dep.kind == "cli":
            return "mode_chat_excludes_cli"
        if _ctx_exceeded(dep, req):
            return "ctx_exceeded"
        if req.has_tools and caps.get("tools") is not True:
            return "tools_unsupported"
        if req.has_images and caps.get("vision") is not True:
            return "vision_unsupported"
        try:
            self._registry.module_for(dep)
        except AdapterUnavailableError:
            return "adapter_unavailable"
        return None

    async def _health(self, dep: Deployment, probe: bool) -> str | None:
        """回 `healthy`、`health_unknown`,不健康回 None。"""
        cached = self.health_cache.get(dep.id)
        if cached is None:
            if not probe:
                return HEALTH_UNKNOWN
            try:
                cached = bool(await self._registry.get(dep).health(dep))
            except Exception:  # noqa: BLE001 — 探測失敗一律視為不健康
                cached = False
            self.health_cache.put(dep.id, cached)
        return HEALTHY if cached else None

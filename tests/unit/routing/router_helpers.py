"""test_router.py 的輔助(CHG-20261030-02 T12)。

- `ScriptedRegistry`:鴨子型別的 AdapterRegistry;每個成員的 `call()` 依腳本回傳 CallResult
  (或丟例外),並記錄 `(dep_id, effort, req)`。
- `FixedEstimator`:固定預估,不碰 LiteLLM。
- `ok()`/`fail()`:建 CallResult。
"""
from __future__ import annotations

from typing import Any

from spillway.adapters.base import AdapterUnavailableError
from spillway.domain import CallResult, Deployment
from spillway.estimate import Estimate

EST = Estimate(tokens_in=10, out_p50=500, out_p90=2000, cost_usd_p90=0.01)


def ok(text: str = "hello", **kw: Any) -> CallResult:
    base = dict(ok=True, text=text, tokens_in=11, tokens_out=7, cost_usd=0.001, status="ok",
                error=None)
    base.update(kw)
    return CallResult(**base)


def fail(status: str = "error", error: str = "boom") -> CallResult:
    return CallResult(ok=False, text="", tokens_in=None, tokens_out=None, cost_usd=None,
                      status=status, error=error)


class FixedEstimator:
    def __init__(self, est: Estimate = EST) -> None:
        self.est = est
        self.calls: list[tuple[str, str | None]] = []

    def estimate(self, dep, req, route_name):
        self.calls.append((dep.id, route_name))
        return self.est


class _Adapter:
    def __init__(self, reg: "ScriptedRegistry") -> None:
        self._reg = reg

    async def call(self, dep: Deployment, req, effort):
        self._reg.calls.append((dep.id, effort, req))
        script = self._reg.script.get(dep.id, ok())
        r = script.pop(0) if isinstance(script, list) else script
        if isinstance(r, BaseException):
            raise r
        return r

    async def health(self, dep: Deployment) -> bool:
        self._reg.health_calls.append(dep.id)
        return self._reg.healthy.get(dep.id, True)


class ScriptedRegistry:
    def __init__(self, *, unavailable_providers: dict[str, str] | None = None) -> None:
        self._unavailable = dict(unavailable_providers or {})
        self.script: dict[str, Any] = {}
        self.calls: list[tuple[str, str | None, Any]] = []
        self.healthy: dict[str, bool] = {}
        self.health_calls: list[str] = []
        self._adapter = _Adapter(self)

    def called_ids(self) -> list[str]:
        return [c[0] for c in self.calls]

    def unavailable(self) -> dict[str, str]:
        return dict(self._unavailable)

    def module_for(self, dep: Deployment) -> object:
        if dep.provider in self._unavailable:
            raise AdapterUnavailableError(dep.provider, self._unavailable[dep.provider])
        return object()

    def get(self, dep: Deployment) -> _Adapter:
        self.module_for(dep)
        return self._adapter

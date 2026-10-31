"""tests/unit/routing 的輔助函式(CHG-20261030-02 T11 擁有;T12 的 test_router.py 可重用)。

- `make_dep(db, id, kind=..., ...)`:建立並存入一個成員,回傳讀回的 `Deployment`。
- `StubRegistry`:鴨子型別的 AdapterRegistry,`module_for`/`get` 與 health 結果、呼叫次數可控。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from spillway.adapters.base import AdapterUnavailableError
from spillway.domain import Budget, Deployment
from spillway.store import repo


def make_dep(
    db: sqlite3.Connection,
    dep_id: str,
    *,
    kind: str = "api",
    model_name: str | None = None,
    provider: str | None = None,
    model: str = "gpt-4o-mini",
    tags: tuple[str, ...] = (),
    enabled: bool = True,
    capabilities: dict[str, Any] | None = None,
    budget: Budget | None = None,
    params: dict[str, Any] | None = None,
) -> Deployment:
    if provider is None:
        provider = {"api": "openai", "cli": "claude-cli", "local": "hosted_vllm"}[kind]
    dep = Deployment(
        id=dep_id,
        model_name=model_name or dep_id,
        kind=kind,
        provider=provider,
        model=model,
        params=params or {},
        credential_ref=None,
        capabilities=capabilities if capabilities is not None else {
            "ctx": 100000, "tools": True, "vision": True, "efforts": ["low", "medium", "high"]},
        enabled=enabled,
        tags=tags,
        budget=budget,
    )
    repo.save_deployment(db, dep)
    loaded = repo.load_deployment(db, dep_id)
    assert loaded is not None
    return loaded


class _StubAdapter:
    def __init__(self, registry: "StubRegistry") -> None:
        self._reg = registry

    async def call(self, dep, req, effort):  # pragma: no cover - 路由元件不呼叫
        raise AssertionError("T11 元件不可呼叫 adapter.call")

    async def health(self, dep: Deployment) -> bool:
        self._reg.health_calls.append(dep.id)
        result = self._reg.healthy.get(dep.id, True)
        if isinstance(result, BaseException):
            raise result
        return result


class StubRegistry:
    """`unavailable_providers` 中的 provider 由 `module_for` 丟 AdapterUnavailableError。"""

    def __init__(self, *, unavailable_providers: dict[str, str] | None = None) -> None:
        self._unavailable = dict(unavailable_providers or {})
        self.healthy: dict[str, Any] = {}
        self.health_calls: list[str] = []
        self._adapter = _StubAdapter(self)

    def unavailable(self) -> dict[str, str]:
        return dict(self._unavailable)

    def module_for(self, dep: Deployment) -> object:
        if dep.provider in self._unavailable:
            raise AdapterUnavailableError(dep.provider, self._unavailable[dep.provider])
        return object()

    def get(self, dep: Deployment) -> _StubAdapter:
        self.module_for(dep)
        return self._adapter

"""`/api/adapters`(CHG-20261101-02 決策 9)。"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Request

from spillway import credentials
from spillway.adapters.base import (
    AdapterContext,
    AdapterUnavailableError,
    ModuleInfo,
    available_models,
)
from spillway.api.deps import request_conn
from spillway.domain import Deployment
from spillway.store import repo

from .common import _clock, router


# ================================================================ /api/adapters


def _module_counts(registry: Any, modules: list[ModuleInfo],
                   deps: list[tuple[str, str]]) -> dict[str, int]:
    """每個模組的現有成員數:可用 provider 依 `registry.module_for`(含後備模組),
    無法解析時以模組宣告(或推得)的 providers 比對。"""
    counts = {m.name: 0 for m in modules}
    for kind, provider in deps:
        probe = Deployment(id="", model_name="", kind=kind, provider=provider, model="")
        try:
            name = registry.module_for(probe).name
        except AdapterUnavailableError:
            name = next((m.name for m in modules if provider in m.providers), None)
        if name in counts:
            counts[name] += 1
    return counts


@router.get("/adapters")
async def list_adapters(request: Request) -> dict[str, Any]:
    """已發現的 adapter 模組(spec 9.2.2;design D13 的形狀):

    `{"modules": [{module, builtin, providers[], kinds[], access[], cardinality, fallback_for_kinds[],
    existing_members, available, unavailable_reason, models[], models_error}]}`

    - 依 `registry.modules()` 的順序(內建模組在前,含載入失敗者)。
    - `access` 為清單(manifest 允許多值,例如 litellm 的 `["api", "local"]`)。
    - 載入失敗的模組:`available` false、`unavailable_reason` 為原因、`kinds`/`access`/`models` 為空、
      `cardinality` null;`providers` 為推得值(決策 14)。
    - `existing_members`:目前由此模組處理的成員數(UI 依 `cardinality` 判斷 single 是否已滿)。
    - `models`:`available_models()`——`models.yaml` 合併 `list_models()`(同 id 以 yaml 為準);
      查詢失敗時只回 yaml,原因放 `models_error`。litellm 模組的 local 端點取自環境變數
      `SPILLWAY_LOCAL_API_BASE`(決策 25,由模組自行讀取)。各模組的查詢並行執行。
    """
    registry = request.app.state.registry
    ctx = AdapterContext(credentials=credentials.resolve, clock=_clock(request))
    with request_conn(request) as conn:
        deps = repo.list_kinds_providers(conn)
    modules = registry.modules()
    counts = _module_counts(registry, modules, deps)
    listings = await asyncio.gather(*(
        available_models(m, ctx) for m in modules if m.available))
    by_name = dict(zip((m.name for m in modules if m.available), listings))
    out = []
    for m in modules:
        man = m.manifest if m.available else None
        listing = by_name.get(m.name)
        out.append({
            "module": m.name,
            "builtin": m.builtin,
            "providers": list(m.providers),
            "kinds": list(man.kinds) if man else [],
            "access": list(man.access) if man else [],
            "cardinality": man.cardinality if man else None,
            "fallback_for_kinds": list(man.fallback_for_kinds) if man else [],
            "existing_members": counts.get(m.name, 0),
            "available": m.available,
            "unavailable_reason": None if m.available else m.reason,
            "models": [x.to_dict() for x in listing.models] if listing else [],
            "models_error": listing.error if listing else None,
        })
    return {"modules": out}

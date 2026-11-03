"""UI-1 總覽的種資料 helper(CHG-20261103-01 T4;決策 22)。

成員與 route 經 `/api` 建立;冷卻沒有 `/api` 寫入端點,經 `tests/helpers/ui.db_execute` 寫入。

- `member_body(dep_id, *, kind="api", tags=None, budget=None, **kw)`:`POST /api/deployments` 的 body
  (api 成員的憑證參照為 `env:SPILLWAY_TEST_KEY`,只是參照,不含值)。
- `add_member(gw, dep_id, **kw)`、`add_route(gw, name, tag)`。
- `set_cooldown(gw, dep_id, until, reason)`。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from spillway.clock import to_iso
from tests.helpers import TEST_KEY_ENV
from tests.helpers.ui import api, db_execute

__all__ = ["add_member", "add_route", "member_body", "set_cooldown"]


def member_body(dep_id: str, *, kind: str = "api", tags: list[str] | None = None,
                budget: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"id": dep_id, "model_name": dep_id, "kind": kind, "tags": tags or ["r"]}
    if kind == "api":
        body.update(provider="openai", model="gpt-4o-mini", credential_ref=f"env:{TEST_KEY_ENV}",
                    capabilities={"ctx": 100000, "tools": True, "vision": True,
                                  "efforts": ["low", "medium", "high"]})
    else:
        body.update(provider="hosted_vllm", model="local-model",
                    capabilities={"ctx": 32768, "tools": False, "vision": False, "efforts": []})
    if budget is not None:
        body["budget"] = budget
    body.update(kw)
    return body


def add_member(gw: Any, dep_id: str, **kw: Any) -> dict[str, Any]:
    return api(gw, "POST", "/api/deployments", member_body(dep_id, **kw)).json()


def add_route(gw: Any, name: str, tag: str = "r") -> dict[str, Any]:
    return api(gw, "POST", "/api/routes", {"name": name, "target": {"tags": {"any": [tag]}}}).json()


def set_cooldown(gw: Any, dep_id: str, until: datetime, reason: str) -> None:
    db_execute(gw, "INSERT INTO cooldowns (deployment_id, exhausted_until, reason) VALUES (?, ?, ?) "
                   "ON CONFLICT(deployment_id) DO UPDATE SET exhausted_until = excluded.exhausted_until, "
                   "reason = excluded.reason",
               [dep_id, to_iso(until), reason])

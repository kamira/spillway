"""UI-4 Route 畫面的種資料與頁面操作(CHG-20261103-01 T6;決策 14、22)。

(CHG-20261107-03 T2 由 `tests/helpers/ui_routes.py` 複製改寫成黑箱版;原檔已於 T5 改用本模組後刪除,4e2ce46)
`tests/ui/test_routes.py` 與 behave 步驟(情境 6)共用;只用 Playwright **sync** API 與同步 httpx
(`tests.blackbox.ui.api`);冷卻經測試控制介面 `POST /__test__/cooldowns`(覆寫)。

種資料
- `api_body(dep_id, fake_api, *, tags=("r",), efforts=("low","medium","high"), enabled=True)`、
  `local_body(dep_id, fake_api, *, tags=("r",))`:`POST /api/deployments` 的 body。
- `add_members(gw, *bodies)`;`create_route(gw, name, target, *, mode="task", **kw)`;
  `put_order(gw, name, slots)`(slots 為 `[[id | (id, effort, weight)], …]`);
  `set_enabled(gw, dep_id, enabled)`(先 GET 再 PUT 全量);`set_cooldown(gw, dep_id, until, reason)`。
- `route_order(gw, name) -> list[list[str]]`、`route_order_full(gw, name)`、`effective_slots(gw, name)`。

頁面
- `open_editor(page, gw, name)`:開啟 `route.html?name=…` 並等到編輯器繪製完成(`#editor[data-state=ready]`)。
- `editor_slots(page) -> list[list[str]]`:畫面的 slot 結構。
- `card(page, dep_id)`、`slot(page, index)`、`slot_head(page, index)`、`new_slot_zone(page)`。
- `drag_card(page, dep_id, to)`(to 為 slot 索引或 `"new"`)、`drag_slot(page, src, dst)`(把 slot `src`
  整列拖到 slot `dst` 上:往上拖時放在 `dst` 之前)。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import quote

from playwright.sync_api import Locator, Page, expect

from tests.blackbox.control import iso
from tests.blackbox.mask import TEST_KEY_ENV
from tests.blackbox.ui import add_route, api, drag, goto

__all__ = ["add_members", "api_body", "card", "create_route", "drag_card", "drag_slot", "editor_slots",
           "effective_slots", "local_body", "new_slot_zone", "open_editor", "put_order", "route_order",
           "route_order_full", "set_cooldown", "set_enabled", "slot", "slot_head"]


# ---- 種資料 -------------------------------------------------------------------------------------

def api_body(dep_id: str, fake_api: Any, *, tags: Iterable[str] = ("r",),
             efforts: Iterable[str] = ("low", "medium", "high"), enabled: bool = True) -> dict[str, Any]:
    return {
        "id": dep_id, "model_name": dep_id, "kind": "api", "provider": "openai", "model": "gpt-4o-mini",
        "credential_ref": f"env:{TEST_KEY_ENV}",
        "capabilities": {"ctx": 100000, "tools": True, "vision": True, "efforts": list(efforts)},
        "params": {"api_base": fake_api.base_url + "/v1"},
        "tags": list(tags), "enabled": enabled,
    }


def local_body(dep_id: str, fake_api: Any, *, tags: Iterable[str] = ("r",)) -> dict[str, Any]:
    return {
        "id": dep_id, "model_name": dep_id, "kind": "local", "provider": "hosted_vllm", "model": "local-model",
        "capabilities": {"ctx": 32768, "tools": False, "vision": False, "efforts": []},
        "params": {"api_base": fake_api.base_url + "/v1"},
        "tags": list(tags),
    }


def add_members(gw: Any, *bodies: dict[str, Any]) -> None:
    for body in bodies:
        api(gw, "POST", "/api/deployments", body, expect=(201,))


def create_route(gw: Any, name: str, target: dict[str, Any], *, mode: str = "task", **kw: Any) -> dict[str, Any]:
    return add_route(gw, name, target=target, mode=mode, **kw)


def _entry(e: str | Sequence[Any]) -> dict[str, Any]:
    if isinstance(e, str):
        return {"deployment": e}
    dep, effort, weight = e
    return {"deployment": dep, "effort": effort, "weight": weight}


def put_order(gw: Any, name: str, slots: Sequence[Sequence[str | Sequence[Any]]]) -> dict[str, Any]:
    body = {"slots": [[_entry(e) for e in s] for s in slots]}
    return api(gw, "PUT", f"/api/routes/{quote(name, safe='')}/order", body, expect=(200,)).json()


def set_enabled(gw: Any, dep_id: str, enabled: bool) -> None:
    dep = api(gw, "GET", f"/api/deployments/{quote(dep_id, safe='')}", expect=(200,)).json()
    for k in ("cooling_until", "cooling_reason"):
        dep.pop(k, None)
    dep["enabled"] = enabled
    api(gw, "PUT", f"/api/deployments/{quote(dep_id, safe='')}", dep, expect=(200,))


def set_cooldown(gw: Any, dep_id: str, until: datetime, reason: str = "rate_limited") -> str:
    """經 `POST /__test__/cooldowns` 覆寫冷卻(沒有設定冷卻的 `/api`);回傳寫入的 ISO 字串
    (`%Y-%m-%dT%H:%M:%SZ`,UTC)。"""
    text = iso(until)
    gw.control.cooldown(dep_id, text, reason)
    return text


def route_order_full(gw: Any, name: str) -> list[list[dict[str, Any]]]:
    return api(gw, "GET", f"/api/routes/{quote(name, safe='')}", expect=(200,)).json()["order"]


def route_order(gw: Any, name: str) -> list[list[str]]:
    return [[e["deployment"] for e in s] for s in route_order_full(gw, name)]


def effective_slots(gw: Any, name: str) -> list[list[str]]:
    eff = api(gw, "GET", f"/api/routes/{quote(name, safe='')}/effective-order", expect=(200,)).json()
    return [[e["deployment"] for e in s["entries"]] for s in eff["slots"]]


# ---- 頁面 ---------------------------------------------------------------------------------------

def open_editor(page: Page, gw: Any, name: str) -> None:
    goto(page, gw, f"/ui/route.html?name={quote(name, safe='')}")
    expect(page.locator("#editor")).to_have_attribute("data-state", "ready")


def editor_slots(page: Page) -> list[list[str]]:
    return page.evaluate("""() => [...document.querySelectorAll('#slots .slot')].map(
        (s) => [...s.querySelectorAll('.member[data-deployment]')].map((m) => m.dataset.deployment))""")


def slot(page: Page, index: int) -> Locator:
    return page.locator(f'#slots .slot[data-slot-index="{index}"]')


def slot_head(page: Page, index: int) -> Locator:
    return slot(page, index).locator(".slothead")


def card(page: Page, dep_id: str) -> Locator:
    return page.locator(f'#slots .member[data-deployment="{dep_id}"]')


def new_slot_zone(page: Page) -> Locator:
    return page.locator("#new-slot")


def drag_card(page: Page, dep_id: str, to: int | str) -> None:
    target = new_slot_zone(page) if to == "new" else slot(page, int(to))
    drag(page, card(page, dep_id).locator(".member-head"), target)


def drag_slot(page: Page, src: int, dst: int) -> None:
    """slot `src` 整列拖到 slot `dst` 上:往上拖時放在 `dst` 之前,往下拖時放在 `dst` 之後。"""
    drag(page, slot_head(page, src), slot(page, dst))

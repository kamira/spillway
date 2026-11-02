"""`/api/routes`(含手動排序 order;CHG-20261101-02 決策 9)。

route 的建立、更新、刪除與 order 的儲存/還原在交易 commit **之後**呼叫 `groups.tools_changed(request)`
(`app.state.mcp_events.changed()`:對 MCP session 送 `notifications/tools/list_changed`;
CHG-20261102-01 決策 13)。它是同步、立即返回的,不拖住管理 API。"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from fastapi import Request, Response

from spillway.api import ApiError, redact_echo
from spillway.api.deps import request_conn
from spillway.routing import members_of
from spillway.store import repo
from spillway.store.db import transaction

from .common import _invalid, _not_found, router
from .groups import tools_changed
from .models import OrderIn, RouteIn


def _order_out(targets: list[repo.RouteTarget]) -> list[list[dict[str, Any]]]:
    slots: dict[int, list[dict[str, Any]]] = {}
    for t in targets:   # 已依 (slot, position, id) 排序
        slots.setdefault(t.slot, []).append(
            {"deployment": t.deployment_id, "effort": t.effort, "weight": t.weight})
    return [slots[k] for k in sorted(slots)]


def _route_out(conn: sqlite3.Connection, r: repo.Route) -> dict[str, Any]:
    """`{name, target, mode, tags[], record_context, budget_precheck, order}`;`order` 為手動排序
    (同 9.3 的 slots 形狀,每筆 `{deployment, effort, weight}`),未手動排序時為 `[]`。"""
    return {"name": r.name, "target": r.target, "mode": r.mode, "tags": list(r.tags),
            "record_context": r.record_context, "budget_precheck": r.budget_precheck,
            "order": _order_out(repo.list_route_targets(conn, r.name))}


def _load_route(conn: sqlite3.Connection, name: str) -> repo.Route:
    r = repo.get_route(conn, name)
    if r is None:
        raise _not_found(f"route {name}")
    return r


def _check_group_exists(conn: sqlite3.Connection, target: Mapping[str, Any]) -> None:
    if "group" in target and repo.get_group(conn, target["group"]) is None:
        raise _invalid("body.target.group", redact_echo(f"群組 {target['group']} 不存在"))


@router.get("/routes")
async def list_routes(request: Request) -> dict[str, Any]:
    """`{"routes": [route, …]}`(依 name 排序;格式同單一 route)。"""
    with request_conn(request) as conn:
        return {"routes": [_route_out(conn, r) for r in repo.list_routes(conn)]}


@router.post("/routes", status_code=201)
async def create_route(request: Request, body: RouteIn) -> dict[str, Any]:
    """建立 route;`{group}` 目標的群組必須存在(否則 422);重複 name → 409 `conflict`。"""
    if body.name is None:
        raise _invalid("body.name", "Field required")
    with request_conn(request) as conn:
        with transaction(conn):
            if repo.get_route(conn, body.name) is not None:
                raise ApiError(409, "conflict", redact_echo(f"route 已存在:{body.name}"))
            _check_group_exists(conn, body.target)
            repo.save_route(conn, body.to_route(body.name))
        tools_changed(request)
        return _route_out(conn, _load_route(conn, body.name))


@router.get("/routes/{name}")
async def get_route(request: Request, name: str) -> dict[str, Any]:
    with request_conn(request) as conn:
        return _route_out(conn, _load_route(conn, name))


@router.put("/routes/{name}")
async def update_route(request: Request, name: str, body: RouteIn) -> dict[str, Any]:
    """整筆取代 route 設定(未給的欄位回到預設);既有手動排序保留(目標變更後不再屬於目標的成員,
    由 Router 依目標過濾,不在此刪除)。"""
    if body.name is not None and body.name != name:
        raise _invalid("body.name", "不可變更 route 名稱(必須與路徑相同)")
    with request_conn(request) as conn:
        with transaction(conn):
            _load_route(conn, name)
            _check_group_exists(conn, body.target)
            repo.save_route(conn, body.to_route(name))
        tools_changed(request)
        return _route_out(conn, _load_route(conn, name))


@router.delete("/routes/{name}", status_code=204)
async def delete_route(request: Request, name: str) -> Response:
    """刪除 route 與其 route_targets。回 204。"""
    with request_conn(request) as conn:
        if not repo.delete_route(conn, name):   # repo 自己的交易已 commit
            raise _not_found(f"route {name}")
    tools_changed(request)
    return Response(status_code=204)


@router.put("/routes/{name}/order")
async def put_route_order(request: Request, name: str, body: OrderIn) -> dict[str, Any]:
    """儲存拖曳結果(spec 9.3)。成員不存在或不屬 route 目標 → 422 `member_not_in_target`
    (message 列出成員 id);以 `repo.replace_route_targets` 單一交易取代全部 route_targets
    (9.3.2;驗證失敗時舊資料不變)。回 route(含新的 `order`)。`slots` 為空清單等同還原預設。"""
    with request_conn(request) as conn:
        with transaction(conn):
            route = _load_route(conn, name)
            members = {d.id for d in members_of(route.target, repo.list_deployments(conn),
                                                repo.list_groups(conn))}
            outside = [e.deployment for slot in body.slots for e in slot
                       if e.deployment not in members]
            if outside:
                raise ApiError(422, "member_not_in_target",
                               redact_echo(f"成員不屬於 route {name} 的目標:{', '.join(outside)}"))
            repo.replace_route_targets(conn, name, [
                repo.RouteTarget(deployment_id=e.deployment, slot=i + 1, position=j,
                                 effort=e.effort, weight=e.weight)
                for i, slot in enumerate(body.slots) for j, e in enumerate(slot)
            ])
        tools_changed(request)
        return _route_out(conn, route)


@router.delete("/routes/{name}/order", status_code=204)
async def delete_route_order(request: Request, name: str) -> Response:
    """還原預設順序(spec 4.3.4):刪除該 route 的全部 route_targets。回 204。"""
    with request_conn(request) as conn:
        with transaction(conn):
            _load_route(conn, name)
            repo.replace_route_targets(conn, name, [])
    tools_changed(request)
    return Response(status_code=204)

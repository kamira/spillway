"""`/api/groups`(CHG-20261101-02 決策 9)。

群組的建立、更新、刪除在交易 commit **之後**呼叫 `common.tools_changed(request)`(MCP
`notifications/tools/list_changed`;CHG-20261102-01 決策 13)。"""
from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import Request, Response

from spillway.api import ApiError, redact_echo
from spillway.api.deps import request_conn
from spillway.domain import Deployment
from spillway.routing import members_of
from spillway.routing.target import normalize_selector
from spillway.store import repo
from spillway.store.db import transaction

from .common import _not_found, invalid, router, tools_changed
from .models import GroupIn, GroupUpdate, group_name_problem

def _member_brief(d: Deployment) -> dict[str, Any]:
    return {"id": d.id, "model_name": d.model_name, "kind": d.kind, "provider": d.provider,
            "enabled": d.enabled}


def _group_out(g: repo.Group, deps: list[Deployment]) -> dict[str, Any]:
    sel = normalize_selector(g.selector)
    return {"name": g.name, "selector": sel,
            "members": [_member_brief(d) for d in members_of({"tags": sel}, deps)]}


def _load_group(conn: sqlite3.Connection, name: str) -> repo.Group:
    g = repo.get_group(conn, name)
    if g is None:
        raise _not_found(f"群組 {name}")
    return g


@router.get("/groups")
async def list_groups(request: Request) -> dict[str, Any]:
    """`{"groups": [群組, …]}`(依 name 排序;格式同單一群組)。"""
    with request_conn(request) as conn:
        deps = repo.list_deployments(conn)
        return {"groups": [_group_out(g, deps) for g in repo.list_groups(conn)]}


@router.post("/groups", status_code=201)
async def create_group(request: Request, body: GroupIn) -> dict[str, Any]:
    """建立群組;重複 name → 409 `conflict`;name 為保留字 `all` → 422 `validation_error`
    (CHG-20261102-01 決策 22:`/mcp/all` 暴露全部 route)。群組沒有改名端點(`PUT` 只取代選擇器)。"""
    problem = group_name_problem(body.name)
    if problem is not None:
        raise invalid("body.name", problem)
    with request_conn(request) as conn:
        with transaction(conn):
            if repo.get_group(conn, body.name) is not None:
                raise ApiError(409, "conflict", redact_echo(f"群組已存在:{body.name}"))
            repo.save_group(conn, repo.Group(body.name, body.selector.model_dump()))
        tools_changed(request)
        return _group_out(_load_group(conn, body.name), repo.list_deployments(conn))


# 註記:宣告在 `/groups/{name}` 之前,防範日後新增 `POST /groups/{name}` 時吃掉 `preview`(AM-13)
@router.post("/groups/preview")
async def preview_group(request: Request, body: GroupUpdate) -> dict[str, Any]:
    """選擇器預覽(CHG-20261103-01 決策 10 ①):body `{selector: {all, any, none}}`(同 `PUT` 的
    `GroupUpdate`,未知鍵 → 422);回 `{members: [...]}`,形狀與 `GET /api/groups/{name}` 的
    `members` 相同(含停用者,依 id 排序);空選擇器符合全部成員。不寫入。"""
    sel = normalize_selector(body.selector.model_dump())
    with request_conn(request) as conn:
        deps = repo.list_deployments(conn)
    return {"members": [_member_brief(d) for d in members_of({"tags": sel}, deps)]}


@router.get("/groups/{name}")
async def get_group(request: Request, name: str) -> dict[str, Any]:
    """`{name, selector: {all, any, none}, members: [{id, model_name, kind, provider, enabled}]}`;
    `members` 為目前符合選擇器的成員預覽(含停用者,依 id 排序)。"""
    with request_conn(request) as conn:
        return _group_out(_load_group(conn, name), repo.list_deployments(conn))


@router.put("/groups/{name}")
async def update_group(request: Request, name: str, body: GroupUpdate) -> dict[str, Any]:
    """取代選擇器;不存在 → 404。"""
    with request_conn(request) as conn:
        with transaction(conn):
            _load_group(conn, name)
            repo.save_group(conn, repo.Group(name, body.selector.model_dump()))
        tools_changed(request)
        return _group_out(_load_group(conn, name), repo.list_deployments(conn))


@router.delete("/groups/{name}", status_code=204)
async def delete_group(request: Request, name: str) -> Response:
    """刪除群組;仍被 route 參照 → 409 `conflict`(message 列出 route 名)。回 204。"""
    with request_conn(request) as conn:
        with transaction(conn):
            _load_group(conn, name)
            refs = [r.name for r in repo.list_routes(conn) if r.target.get("group") == name]
            if refs:
                raise ApiError(409, "conflict", redact_echo(f"群組 {name} 仍被 route 參照:{', '.join(refs)}"))
            repo.delete_group(conn, name)
    tools_changed(request)
    return Response(status_code=204)

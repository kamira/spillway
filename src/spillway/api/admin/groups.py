"""`/api/groups`(CHG-20261101-02 決策 9)。"""
from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import Request, Response

from spillway.api import ApiError, redact_echo
from spillway.api.deps import request_conn
from spillway.domain import Deployment
from spillway.routing import members_of
from spillway.store import repo
from spillway.store.db import transaction

from .common import _not_found, router
from .models import GroupIn, GroupUpdate


def _member_brief(d: Deployment) -> dict[str, Any]:
    return {"id": d.id, "model_name": d.model_name, "kind": d.kind, "provider": d.provider,
            "enabled": d.enabled}


def _group_out(g: repo.Group, deps: list[Deployment]) -> dict[str, Any]:
    sel = {k: list(g.selector.get(k) or []) for k in ("all", "any", "none")}
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
    """建立群組;重複 name → 409 `conflict`。"""
    with request_conn(request) as conn:
        with transaction(conn):
            if repo.get_group(conn, body.name) is not None:
                raise ApiError(409, "conflict", redact_echo(f"群組已存在:{body.name}"))
            repo.save_group(conn, repo.Group(body.name, body.selector.model_dump()))
        return _group_out(_load_group(conn, body.name), repo.list_deployments(conn))


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
    return Response(status_code=204)

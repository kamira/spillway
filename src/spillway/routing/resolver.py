"""請求解析(spec 4.1.1–4.1.4;CHG-20261030-02 T11;決策 18)。

`Resolver(conn).resolve(model) -> Target`,依序:

1. `local/<x>`(4.1.1):在 kind = local 的成員中先以 id、再以 `model_name` 比對 `<x>`。
   以 id 命中者優先;id 無命中而 `model_name` 命中多個時取 id 字母序第一個(單一結果)。
   找不到 → `RouteNotFound`。不看 `enabled`(直呼不經路由,由 Router 決定如何處理)。
2. `route:<name>`(4.1.2):必須是既有 route,否則 `RouteNotFound`(不再退回 model_name 比對)。
3. 與 route 名稱相同(4.1.2)→ 該 route。route 名稱優先於成員 `model_name`。
4. 等於任一成員的 `model_name`(4.1.3)→ 隱含 route:成員為全部同名成員(不分 kind;
   `chat` 模式會由 CandidateFilter 篩掉 cli),mode `chat`、無 route_targets(採預設順序)、
   不做 precheck、record_context 沿用全域;帳本 `route_name` 為該 model_name(決策 18)。
5. 皆不符 → `RouteNotFound`(4.1.4,HTTP 404 `route_not_found`,由 T13 轉換)。

route 成員以 `target.members_of()` 對全部成員(含停用者)比對;成員依 id 排序。
本模組只用 `store.repo` 既有查詢,沒有自寫 SQL。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from spillway.domain import Deployment
from spillway.routing.target import members_of
from spillway.store import repo
from spillway.store.repo import Route, RouteTarget

LOCAL_PREFIX = "local/"
ROUTE_PREFIX = "route:"

TARGET_KINDS = ("local_direct", "route", "implicit")

ConnSource = sqlite3.Connection | Callable[[], sqlite3.Connection]


class RouteNotFound(LookupError):
    """4.1.4:`model` 無法解析。`code` 固定為 `route_not_found`。"""

    code = "route_not_found"

    def __init__(self, model: str) -> None:
        super().__init__(f"找不到 route 或成員:{model}")
        self.model = model


@dataclass(frozen=True)
class Target:
    """`resolve()` 的結果。

    - `kind`:`local_direct`(4.1.1)、`route`(4.1.2)、`implicit`(4.1.3)。
    - `model`:請求原本的 `model` 字串。
    - `route_name`:帳本與預估用的 route 名稱(決策 18):route 為其名稱、implicit 為
      model_name、local_direct 為 None。
    - `members`:目標成員(依 id 排序,含停用者);local_direct 恰為一個。
    - `mode`:`chat` / `task`(local_direct 與 implicit 為 `chat`)。
    - `budget_precheck`、`record_context`:來自 route;其他 kind 為 False / None(沿用全域)。
    - `route`、`route_targets`:只有 kind = route 才有(route_targets 依 slot、position 排序)。
    """

    kind: str
    model: str
    route_name: str | None
    members: tuple[Deployment, ...]
    mode: str = "chat"
    budget_precheck: bool = False
    record_context: bool | None = None
    route: Route | None = None
    route_targets: tuple[RouteTarget, ...] = ()

    @property
    def deployment(self) -> Deployment:
        """local_direct 的唯一成員;其他 kind 呼叫時丟 ValueError。"""
        if self.kind != "local_direct":
            raise ValueError("只有 local_direct 有單一成員")
        return self.members[0]


class Resolver:
    """`conn` 可為連線或零參數連線工廠(每次 `resolve()` 取一次;本類別不關閉連線)。"""

    def __init__(self, conn: ConnSource) -> None:
        self._conn_src = conn

    def _conn(self) -> sqlite3.Connection:
        src = self._conn_src
        return src if isinstance(src, sqlite3.Connection) else src()

    def resolve(self, model: str) -> Target:
        conn = self._conn()
        if model.startswith(LOCAL_PREFIX):
            return self._local(conn, model)
        if model.startswith(ROUTE_PREFIX):
            route = repo.get_route(conn, model[len(ROUTE_PREFIX):])
            if route is None:
                raise RouteNotFound(model)
            return self._route(conn, model, route)
        route = repo.get_route(conn, model)
        if route is not None:
            return self._route(conn, model, route)
        same = [d for d in repo.list_deployments(conn) if d.model_name == model]
        if same:
            return Target(kind="implicit", model=model, route_name=model, members=tuple(same))
        raise RouteNotFound(model)

    @staticmethod
    def _local(conn: sqlite3.Connection, model: str) -> Target:
        key = model[len(LOCAL_PREFIX):]
        locals_ = [d for d in repo.list_deployments(conn) if d.kind == "local"]
        hit = next((d for d in locals_ if d.id == key), None)
        if hit is None:
            hit = next((d for d in locals_ if d.model_name == key), None)
        if hit is None:
            raise RouteNotFound(model)
        return Target(kind="local_direct", model=model, route_name=None, members=(hit,))

    @staticmethod
    def _route(conn: sqlite3.Connection, model: str, route: Route) -> Target:
        members = members_of(route.target, repo.list_deployments(conn), repo.list_groups(conn))
        return Target(
            kind="route",
            model=model,
            route_name=route.name,
            members=tuple(members),
            mode=route.mode,
            budget_precheck=route.budget_precheck,
            record_context=route.record_context,
            route=route,
            route_targets=tuple(repo.list_route_targets(conn, route.name)),
        )

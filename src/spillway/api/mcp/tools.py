"""MCP 工具 `ask`、`list_routes`、`quota_status`(CHG-20261102-01 T2;決策 11、12;AM-3、AM-10、AM-12)。

工具定義依「本請求的群組」在每次 `tools/list` 時產生(`tool_definitions(conn, group)`);`mcp_catalog`
用同一個函式,所以兩者逐欄相同(決策 8)。

群組可見的 route(`visible_routes`):群組 `all` 為全部 route;其他群組為 `target.group` 等於群組名的
route(spec 8.1.4)。群組在 session 期間被刪:`tools/list` 回空清單、`ask` 回 `route_not_found`(決策 9)。

`ask`(決策 11)
- 以 `@server.call_tool(validate_input=False)` 註冊(SDK 的 `_tool_cache` 跨群組共用,會拿錯群組的 enum
  驗證);這裡依本請求群組自行驗證:`route` 必須是該群組當下可見的 route、`prompt` 為非空字串、`effort`
  ∈ 五級、不可有其他鍵;失敗 → `validation_error`。
- 執行:`ChatRequest(model=f"route:{name}", messages=[{"role":"user","content":prompt}], effort)` 交
  `Router.route`(`route:` 前綴只解析 route,不會退回同名成員的隱含 route,所以 `model=<成員名>` 繞不過
  群組);帳本列與 `/v1` 同一路徑寫入(`route_name`、slot、花費);route 的 mode 決定是否選 CLI 成員。
- 成功:`content` 一段文字(回應文字);`structuredContent`
  `{text, served_by, slot, cost_usd, tokens_in, tokens_out}`。預估值不經 MCP 露出。
- 錯誤一律回 `CallToolResult(isError=True, content=[TextContent(json)], structuredContent={"error":
  {code, message}, "attempts": [...]?})`:`route_not_found`、`no_candidate`(帶 `attempts`,每項
  `{deployment, status, slot}`)、`validation_error`;`shutting_down` 由 T3 經 `api.drain.run_until_drain`
  接上(決策 18)。非預期例外 → `internal_error`(既有 9.1.2 錯誤碼;訊息只含例外型別)。

`list_routes`(決策 12):`{"routes": [{name, mode, ordered, slots: [{slot, members: [{id, effort, weight}]}]}]}`;
有手動排序時 `ordered` 為 `"manual"`,slot 與成員順序與 `GET /api/routes/{name}` 的 `order` 同源
(`repo.list_route_targets`);沒有時依 spec 4.3.2 以 `routing.OrderPlanner` 產生預設順序,`ordered` 為
`"default"`(chat route 不列 cli 成員,與 CandidateFilter 一致;編號仍依全部成員固定)。

`quota_status`(決策 12):`{"members": [{id, kind, provider, budget_type, remaining, window, cooling_until,
cooling_reason}]}`,範圍為群組選擇器涵蓋的成員(與 `GET /api/groups/{name}` 的預覽同源;`all` 為全部
成員),依 id 排序;推導與 `/api/dashboard` 同為 `spillway.quota.member_quota_rows`。

三個工具的 outputSchema 都是單一 `type: object`、`required: []` 的寬鬆 schema(成功與錯誤結果都相容,
不用 anyOf;AM-3)。

秘密:不記錄 prompt、回應與 token;錯誤訊息不含例外內容。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from mcp import types

from spillway.domain import EFFORTS, ChatRequest
from spillway.quota import member_quota_rows
from spillway.redact import REDACTOR
from spillway.routing import (
    Candidate,
    InvalidEffort,
    OrderPlanner,
    RouteNotFound,
    RouteOutcome,
    members_of,
)
from spillway.store import repo

__all__ = [
    "ALL_GROUP", "TOOL_NAMES", "TOOL_ERROR_CODES", "ASK_OUTPUT_SCHEMA", "LIST_ROUTES_OUTPUT_SCHEMA",
    "QUOTA_STATUS_OUTPUT_SCHEMA", "group_exists", "visible_routes", "tool_definitions", "call_tool",
    "error_result",
]

_log = logging.getLogger(__name__)

ALL_GROUP = "all"
TOOL_NAMES = ("ask", "list_routes", "quota_status")
# 工具層錯誤碼(決策 8 `errors.tool`)
TOOL_ERROR_CODES = ("route_not_found", "no_candidate", "shutting_down", "validation_error")

_ERROR_PROPS: dict[str, Any] = {
    "error": {
        "type": "object",
        "properties": {"code": {"type": "string"}, "message": {"type": "string"}},
    },
}

ASK_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [],
    "properties": {
        "text": {"type": "string", "description": "回應文字"},
        "served_by": {"type": "string", "description": "實際處理的成員 id"},
        "slot": {"type": ["integer", "null"], "description": "成員所在 slot"},
        "cost_usd": {"type": ["number", "null"], "description": "本次花費(USD;無定價時為 null)"},
        "tokens_in": {"type": ["integer", "null"]},
        "tokens_out": {"type": ["integer", "null"]},
        **_ERROR_PROPS,
        "attempts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "deployment": {"type": "string"},
                    "status": {"type": "string"},
                    "slot": {"type": ["integer", "null"]},
                },
            },
        },
    },
}

LIST_ROUTES_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [],
    "properties": {
        "routes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "mode": {"type": "string"},
                    "ordered": {"type": "string", "enum": ["manual", "default"]},
                    "slots": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "slot": {"type": "integer"},
                                "members": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "string"},
                                            "effort": {"type": ["string", "null"]},
                                            "weight": {"type": "string"},
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
        **_ERROR_PROPS,
    },
}

QUOTA_STATUS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [],
    "properties": {
        "members": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string"},
                    "provider": {"type": "string"},
                    "budget_type": {"type": ["string", "null"]},
                    "remaining": {"type": ["number", "null"]},
                    "window": {"type": ["string", "null"]},
                    "cooling_until": {"type": ["string", "null"]},
                    "cooling_reason": {"type": ["string", "null"]},
                },
            },
        },
        **_ERROR_PROPS,
    },
}

_NO_ARGS_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

_QUOTA_FIELDS = ("id", "kind", "provider", "budget_type", "remaining", "window", "cooling_until",
                 "cooling_reason")


# ================================================================ 群組與 route


def group_exists(conn: sqlite3.Connection, group: str) -> bool:
    return group == ALL_GROUP or repo.get_group(conn, group) is not None


def visible_routes(conn: sqlite3.Connection, group: str) -> list[repo.Route]:
    """群組可見的 route(依 name);群組不存在時為空清單。"""
    routes = repo.list_routes(conn)
    if group == ALL_GROUP:
        return routes
    if repo.get_group(conn, group) is None:
        return []
    return [r for r in routes if r.target.get("group") == group]


# ================================================================ 工具定義


def _ask_input_schema(route_names: list[str]) -> dict[str, Any]:
    if route_names:
        route: dict[str, Any] = {"type": "string", "enum": route_names,
                                 "description": "要使用的 route(本群組可見者)"}
    else:   # 空 enum 不可滿足,且 Draft-4/OpenAPI 子集的用戶端會拒絕(決策 11)
        route = {"type": "string", "description": "要使用的 route(本群組目前無 route)"}
    return {
        "type": "object",
        "properties": {
            "route": route,
            "prompt": {"type": "string", "minLength": 1, "description": "交給 route 處理的 prompt"},
            "effort": {"type": "string", "enum": list(EFFORTS),
                       "description": "選填;推理強度(spec 6.1)"},
        },
        "required": ["route", "prompt"],
        "additionalProperties": False,
    }


def tool_definitions(conn: sqlite3.Connection, group: str) -> list[types.Tool]:
    """本群組 `tools/list` 的工具定義;群組不存在時為空清單(決策 9)。"""
    if not group_exists(conn, group):
        return []
    names = [r.name for r in visible_routes(conn, group)]
    route_list = "、".join(names) if names else "目前無 route"
    return [
        types.Tool(
            name="ask",
            title="Ask a route",
            description=f"以群組 {group} 的 route 處理 prompt,回傳回應文字與實際處理的成員、slot、花費。"
                        f"可用 route:{route_list}",
            inputSchema=_ask_input_schema(names),
            outputSchema=ASK_OUTPUT_SCHEMA,
        ),
        types.Tool(
            name="list_routes",
            title="List routes",
            description=f"列出群組 {group} 可見的 route、mode 與各 slot 的成員順序",
            inputSchema=_NO_ARGS_SCHEMA,
            outputSchema=LIST_ROUTES_OUTPUT_SCHEMA,
            annotations=types.ToolAnnotations(readOnlyHint=True),
        ),
        types.Tool(
            name="quota_status",
            title="Quota status",
            description=f"列出群組 {group} 的成員額度類型、剩餘額度與冷卻狀態",
            inputSchema=_NO_ARGS_SCHEMA,
            outputSchema=QUOTA_STATUS_OUTPUT_SCHEMA,
            annotations=types.ToolAnnotations(readOnlyHint=True),
        ),
    ]


# ================================================================ 結果


def error_result(code: str, message: str, *, attempts: list[dict[str, Any]] | None = None
                 ) -> types.CallToolResult:
    """工具錯誤結果(決策 11):`isError=True`,不是 JSON-RPC error。"""
    structured: dict[str, Any] = {"error": {"code": code, "message": message}}
    if attempts is not None:
        structured["attempts"] = attempts
    return types.CallToolResult(
        isError=True,
        content=[types.TextContent(type="text", text=json.dumps(structured, ensure_ascii=False))],
        structuredContent=structured,
    )


def _ok_result(structured: dict[str, Any], text: str | None = None) -> types.CallToolResult:
    if text is None:
        text = json.dumps(structured, ensure_ascii=False)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                structuredContent=structured, isError=False)


def _no_candidate_message(outcome: RouteOutcome) -> str:
    tried = ", ".join(f"{a.deployment}:{a.status}" for a in outcome.attempts)
    excluded = ", ".join(f"{dep}:{reason}" for dep, reason in outcome.excluded)
    parts = ["沒有可用的候選成員", f"嘗試:{tried}" if tried else "嘗試:無"]
    if excluded:
        parts.append(f"被篩:{excluded}")
    text, _ = REDACTOR.redact(";".join(parts))   # 防線:成員 id 與狀態不應含秘密
    return text


# ================================================================ 工具實作


async def call_tool(app: Any, group: str, name: str, arguments: dict[str, Any] | None
                    ) -> types.CallToolResult:
    """`tools/call` 的進入點;一律回 `CallToolResult`(SDK 原樣放行,不做 outputSchema 驗證)。"""
    args = arguments if arguments is not None else {}
    if not isinstance(args, dict):
        return error_result("validation_error", "arguments 必須是物件")
    try:
        if name == "ask":
            return await _ask(app, group, args)
        if name == "list_routes":
            return _no_args(args) or _list_routes(app, group)
        if name == "quota_status":
            return _no_args(args) or _quota_status(app, group)
    except Exception as exc:  # noqa: BLE001 — 不回顯例外內容(可能含 prompt 或上游訊息)
        _log.error("MCP 工具 %s 內部錯誤:%s", name, type(exc).__name__)
        return error_result("internal_error", f"內部錯誤({type(exc).__name__})")
    return error_result("validation_error", f"未知的工具:{REDACTOR.redact(str(name))[0]}")


def _no_args(args: dict[str, Any]) -> types.CallToolResult | None:
    if args:
        return error_result("validation_error", f"此工具不接受參數:{', '.join(sorted(map(str, args)))}")
    return None


async def _ask(app: Any, group: str, args: dict[str, Any]) -> types.CallToolResult:
    extra = sorted(set(map(str, args)) - {"route", "prompt", "effort"})
    if extra:
        return error_result("validation_error", f"不認得的參數:{', '.join(extra)}")
    route, prompt, effort = args.get("route"), args.get("prompt"), args.get("effort")
    if not isinstance(prompt, str) or not prompt.strip():
        return error_result("validation_error", "prompt: 必須是非空字串")
    if effort is not None and (not isinstance(effort, str) or effort not in EFFORTS):
        return error_result("validation_error", f"effort: 只可為 {', '.join(EFFORTS)}")
    if not isinstance(route, str) or not route:
        return error_result("validation_error", "route: 必須是本群組的 route 名稱")

    conn = app.state.conn_factory()
    try:
        if not group_exists(conn, group):   # 群組在 session 期間被刪(決策 9)
            return error_result("route_not_found", f"群組 {group} 已不存在")
        visible = {r.name for r in visible_routes(conn, group)}
    finally:
        conn.close()
    if route not in visible:
        return error_result("validation_error",
                            f"route: {REDACTOR.redact(route)[0]} 不是群組 {group} 的 route")

    req = ChatRequest(model=f"route:{route}", messages=[{"role": "user", "content": prompt}],
                      reasoning_effort=effort)
    try:
        outcome = await app.state.router.route(req)
    except RouteNotFound:   # 驗證後才被刪除
        return error_result("route_not_found", f"找不到 route:{REDACTOR.redact(route)[0]}")
    except InvalidEffort:   # Router 的防線;上面已驗證
        return error_result("validation_error", f"effort: 只可為 {', '.join(EFFORTS)}")

    if outcome.result is None:
        attempts = [{"deployment": a.deployment, "status": a.status, "slot": a.slot}
                    for a in outcome.attempts]
        return error_result("no_candidate", _no_candidate_message(outcome), attempts=attempts)
    result = outcome.result
    text = result.text or ""
    return _ok_result({
        "text": text, "served_by": outcome.served_by, "slot": outcome.slot,
        "cost_usd": result.cost_usd, "tokens_in": result.tokens_in, "tokens_out": result.tokens_out,
    }, text=text)


def _list_routes(app: Any, group: str) -> types.CallToolResult:
    conn = app.state.conn_factory()
    try:
        routes = visible_routes(conn, group)
        deps = repo.list_deployments(conn) if routes else []
        groups = repo.list_groups(conn) if routes else []
        out = []
        for r in routes:
            targets = repo.list_route_targets(conn, r.name)   # 已依 (slot, position, id) 排序
            if targets:
                slots: dict[int, list[dict[str, Any]]] = {}
                for t in targets:
                    slots.setdefault(t.slot, []).append(
                        {"id": t.deployment_id, "effort": t.effort, "weight": t.weight})
                out.append({"name": r.name, "mode": r.mode, "ordered": "manual",
                            "slots": [{"slot": n, "members": slots[n]} for n in sorted(slots)]})
                continue
            members = members_of(r.target, deps, groups)
            pool = [d for d in members if not (r.mode == "chat" and d.kind == "cli")]
            cands = [Candidate(deployment=d, estimate=None) for d in pool]  # type: ignore[arg-type]
            plan = OrderPlanner().plan([], cands, members)
            out.append({"name": r.name, "mode": r.mode, "ordered": "default", "slots": [
                {"slot": s.number,
                 "members": [{"id": e.deployment.id, "effort": e.effort, "weight": e.weight}
                             for e in s.entries]}
                for s in plan]})
    finally:
        conn.close()
    return _ok_result({"routes": out})


def _quota_status(app: Any, group: str) -> types.CallToolResult:
    clock = app.state.clock
    conn = app.state.conn_factory()
    try:
        rows = member_quota_rows(conn, clock, clock.now())
        if group != ALL_GROUP:
            g = repo.get_group(conn, group)
            if g is None:
                rows = []
            else:
                sel = {k: list(g.selector.get(k) or []) for k in ("all", "any", "none")}
                ids = {d.id for d in members_of({"tags": sel}, repo.list_deployments(conn))}
                rows = [r for r in rows if r["id"] in ids]
    finally:
        conn.close()
    return _ok_result({"members": [{k: r[k] for k in _QUOTA_FIELDS} for r in rows]})

"""MCP 工具 `ask`、`list_routes`、`quota_status` 與群組可見性(CHG-20261102-01 T2 L1/L2;決策 9、11、12)。

以 `live_gateway`(真實 uvicorn)+ 官方 SDK 用戶端端到端;成員打假上游 `fake_api`。
資料由 `seed_basic` 直接寫入資料庫(群組 `coding`:route `review`、`review-b`;`ops`:`deploy`;
`chat-x` 不屬任何群組)。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from mcp import types

from spillway.store import repo
from tests.helpers import api_member

pytestmark = pytest.mark.anyio

ALL_ROUTES = ["chat-x", "deploy", "review", "review-b"]


def _enum(tools: list[types.Tool]) -> list[str] | None:
    ask = next(t for t in tools if t.name == "ask")
    return ask.inputSchema["properties"]["route"].get("enum")


def _err(result: types.CallToolResult) -> str:
    assert result.isError is True
    assert result.structuredContent is not None, result.content
    assert "Output validation error" not in result.content[0].text
    assert json.loads(result.content[0].text) == result.structuredContent
    return result.structuredContent["error"]["code"]


# ---- initialize 與 tools/list ------------------------------------------------------------------

async def test_initialize_declares_list_changed(seeded_gateway):
    async with seeded_gateway.mcp_session("coding") as c:
        assert c.init.capabilities.tools is not None
        assert c.init.capabilities.tools.listChanged is True
        assert c.init.serverInfo.name == "spillway"


@pytest.mark.parametrize("group,expected", [
    ("coding", ["review", "review-b"]), ("ops", ["deploy"]), ("all", ALL_ROUTES)])
async def test_tools_list_by_group(seeded_gateway, group, expected):
    async with seeded_gateway.mcp_session(group) as c:
        tools = (await c.session.list_tools()).tools
        assert [t.name for t in tools] == ["ask", "list_routes", "quota_status"]
        assert _enum(tools) == expected
        for t in tools:
            assert t.outputSchema["type"] == "object" and t.outputSchema["required"] == []
            assert "anyOf" not in json.dumps(t.outputSchema)


async def test_group_without_routes_lists_ask_as_plain_string(seeded_gateway):
    with seeded_gateway.db() as conn:
        repo.save_group(conn, repo.Group("empty", {"all": ["zzz"]}))
    async with seeded_gateway.mcp_session("empty") as c:
        tools = (await c.session.list_tools()).tools
        route = next(t for t in tools if t.name == "ask").inputSchema["properties"]["route"]
        assert route["type"] == "string" and "enum" not in route
        assert "目前無 route" in route["description"]
        assert _err(await c.session.call_tool("ask", {"route": "review", "prompt": "hi"})) == \
            "validation_error"


# ---- ask ---------------------------------------------------------------------------------------

async def test_ask_success_structured_content_and_ledger(seeded_gateway, db):
    async with seeded_gateway.mcp_session("coding") as c:
        r = await c.session.call_tool("ask", {"route": "review", "prompt": "hello"})
    assert r.isError is False, r.content
    sc = r.structuredContent
    assert set(sc) == {"text", "served_by", "slot", "cost_usd", "tokens_in", "tokens_out"}
    assert sc["text"] == "fake upstream ok" and r.content[0].text == sc["text"]
    assert sc["served_by"] in {"api-a", "api-ab"}
    rows = repo.list_ledger(db)
    assert len(rows) == 1
    row = rows[0]
    assert (row.route_name, row.deployment_id, row.slot, row.status) == ("review", sc["served_by"], sc["slot"], "ok")
    assert row.cost_usd == pytest.approx(sc["cost_usd"]) and sc["cost_usd"] is not None
    assert (row.tokens_in, row.tokens_out) == (sc["tokens_in"], sc["tokens_out"]) == (11, 7)


async def test_ask_records_payload_when_route_records_context(seeded_gateway, db):
    with seeded_gateway.db() as conn:
        repo.save_route(conn, repo.Route("logged", {"group": "coding"}, record_context=True))
    async with seeded_gateway.mcp_session("coding") as c:
        r = await c.session.call_tool("ask", {"route": "logged", "prompt": "remember-this-prompt"})
    assert r.isError is False, r.content
    row = repo.list_ledger(db)[0]
    payload = repo.get_payload(db, row.id)
    assert payload is not None and "remember-this-prompt" in payload.request_json


async def test_ask_effort_passed(seeded_gateway, db):
    async with seeded_gateway.mcp_session("coding") as c:
        r = await c.session.call_tool("ask", {"route": "review", "prompt": "x", "effort": "high"})
    assert r.isError is False


@pytest.mark.parametrize("args,why", [
    ({"route": "deploy", "prompt": "x"}, "route 在別的群組"),
    ({"route": "chat-x", "prompt": "x"}, "route 不屬任何群組"),
    ({"route": "nope", "prompt": "x"}, "route 不存在"),
    ({"route": "review", "prompt": ""}, "空 prompt"),
    ({"route": "review", "prompt": "   "}, "空白 prompt"),
    ({"route": "review", "prompt": 3}, "prompt 不是字串"),
    ({"route": "review", "prompt": "x", "effort": "ultra"}, "非法 effort"),
    ({"route": "review", "prompt": "x", "effort": "minimal"}, "別名不在 enum"),
    ({"route": "review", "prompt": "x", "extra": 1}, "多餘參數"),
    ({"prompt": "x"}, "缺 route"),
    ({"route": "gpt-4.1-mini", "prompt": "x"}, "成員 model_name(ops 的 api-b)"),
])
async def test_ask_validation_error(seeded_gateway, db, args, why):
    async with seeded_gateway.mcp_session("coding") as c:
        assert _err(await c.session.call_tool("ask", args)) == "validation_error", why
    assert repo.list_ledger(db) == []   # 沒有送出請求


async def test_member_name_does_not_bypass_group_even_on_all(seeded_gateway, db):
    """`model=<成員名>` 不會退回隱含 route:`all` 也只接受 route 名稱。"""
    async with seeded_gateway.mcp_session("all") as c:
        assert _err(await c.session.call_tool("ask", {"route": "gpt-4o-mini", "prompt": "x"})) == \
            "validation_error"
    assert repo.list_ledger(db) == []


async def test_ask_no_candidate(seeded_gateway, fake_api, db):
    for model in ("gpt-4o-mini", "gpt-4.1-nano"):
        fake_api.set_mode(model, "http_500")
    async with seeded_gateway.mcp_session("coding") as c:
        r = await c.session.call_tool("ask", {"route": "review", "prompt": "x"})
    assert _err(r) == "no_candidate"
    attempts = r.structuredContent["attempts"]
    assert {a["deployment"] for a in attempts} == {"api-a", "api-ab"}
    assert all(set(a) == {"deployment", "status", "slot"} and a["status"] == "error" for a in attempts)
    assert "api-a:error" in r.structuredContent["error"]["message"]


async def test_route_rename_then_ask_new_name(seeded_gateway):
    gw = seeded_gateway
    async with gw.mcp_session("coding") as c:
        assert _enum((await c.session.list_tools()).tools) == ["review", "review-b"]
        with gw.http() as http:
            assert http.delete("/api/routes/review-b").status_code == 204
            r = http.post("/api/routes", json={"name": "review-c", "target": {"group": "coding"}})
            assert r.status_code == 201, r.text
        r = await c.session.call_tool("ask", {"route": "review-c", "prompt": "x"})   # 不必先重取清單
        assert r.isError is False, r.content
        assert _err(await c.session.call_tool("ask", {"route": "review-b", "prompt": "x"})) == \
            "validation_error"


async def test_group_deleted_during_session(seeded_gateway):
    gw = seeded_gateway
    async with gw.mcp_session("ops") as c:
        assert _enum((await c.session.list_tools()).tools) == ["deploy"]
        with gw.http() as http:
            assert http.delete("/api/routes/deploy").status_code == 204
            assert http.delete("/api/groups/ops").status_code == 204
        assert (await c.session.list_tools()).tools == []
        assert _err(await c.session.call_tool("ask", {"route": "deploy", "prompt": "x"})) == \
            "route_not_found"
        r = await c.session.call_tool("list_routes", {})
        assert r.isError is False and r.structuredContent == {"routes": []}
    # 新的 session 連不上(群組不存在 → 404)
    with gw.http() as http:
        r = http.post("/mcp/ops", headers=gw.auth_headers(), json={})
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


async def test_two_groups_interleaved_do_not_pollute(seeded_gateway, db):
    gw = seeded_gateway
    async with gw.mcp_session("coding") as a, gw.mcp_session("ops") as b:
        for _ in range(3):
            ta, tb = await asyncio.gather(a.session.list_tools(), b.session.list_tools())
            assert _enum(ta.tools) == ["review", "review-b"]
            assert _enum(tb.tools) == ["deploy"]
            ra, rb, xa, xb = await asyncio.gather(
                a.session.call_tool("ask", {"route": "review", "prompt": "x"}),
                b.session.call_tool("ask", {"route": "deploy", "prompt": "x"}),
                a.session.call_tool("ask", {"route": "deploy", "prompt": "x"}),
                b.session.call_tool("ask", {"route": "review", "prompt": "x"}),
            )
            assert ra.isError is False and rb.isError is False
            assert rb.structuredContent["served_by"] in {"api-b", "api-ab"}
            assert _err(xa) == "validation_error" and _err(xb) == "validation_error"
    assert {r.route_name for r in repo.list_ledger(db)} == {"review", "deploy"}


async def test_chat_route_does_not_pick_cli_member(seeded_gateway, fake_cli, db):
    gw = seeded_gateway
    with gw.http() as http:
        r = http.post("/api/deployments", json={
            "id": "claude-sub", "model_name": "claude-sub", "kind": "cli", "provider": "claude-cli",
            "model": "claude-sonnet-5-5", "params": {"max_concurrency": 1, "timeout": 60},
            "capabilities": {"ctx": 1000000, "tools": False, "vision": False,
                             "efforts": ["low", "medium", "high", "max"]},
            "tags": ["a"]})
        assert r.status_code == 201, r.text
        assert http.post("/api/routes", json={"name": "task-r", "target": {"group": "coding"},
                                              "mode": "task"}).status_code == 201
    async with gw.mcp_session("coding") as c:
        for _ in range(3):
            r = await c.session.call_tool("ask", {"route": "review", "prompt": "x"})   # chat
            assert r.isError is False and r.structuredContent["served_by"] != "claude-sub"
        assert fake_cli.records("claude") == []
        r = await c.session.call_tool("ask", {"route": "task-r", "prompt": "x"})        # task:cli 在 slot 1
        assert r.isError is False, r.content
        assert r.structuredContent["served_by"] == "claude-sub" and r.structuredContent["slot"] == 1
    assert len(fake_cli.records("claude")) == 1


async def test_unknown_tool_and_args_on_no_arg_tools(seeded_gateway):
    async with seeded_gateway.mcp_session("coding") as c:
        assert _err(await c.session.call_tool("nope", {})) == "validation_error"
        assert _err(await c.session.call_tool("list_routes", {"x": 1})) == "validation_error"
        assert _err(await c.session.call_tool("quota_status", {"x": 1})) == "validation_error"


# ---- list_routes -------------------------------------------------------------------------------

async def test_list_routes_manual_matches_admin_order(seeded_gateway, fake_api):
    gw = seeded_gateway
    with gw.http() as http:
        body = {"slots": [[{"deployment": "api-ab", "weight": "high", "effort": "low"}],
                          [{"deployment": "api-a"}]]}
        assert http.put("/api/routes/review/order", json=body).status_code == 200
        admin = http.get("/api/routes/review").json()["order"]
    async with gw.mcp_session("coding") as c:
        r = await c.session.call_tool("list_routes", {})
    assert r.isError is False
    routes = {x["name"]: x for x in r.structuredContent["routes"]}
    assert list(routes) == ["review", "review-b"]
    review = routes["review"]
    assert review["ordered"] == "manual" and review["mode"] == "chat"
    assert [[{"deployment": m["id"], "effort": m["effort"], "weight": m["weight"]} for m in s["members"]]
            for s in review["slots"]] == admin
    assert [s["slot"] for s in review["slots"]] == [1, 2]


async def test_list_routes_default_order(seeded_gateway, fake_cli):
    gw = seeded_gateway
    with gw.db() as conn:
        repo.save_deployment(conn, api_member("api-z", "http://127.0.0.1:9", tags=("b",)))
    async with gw.mcp_session("ops") as c:
        r = await c.session.call_tool("list_routes", {})
    (deploy,) = r.structuredContent["routes"]
    assert deploy["ordered"] == "default"
    # spec 4.3.2:無 cli → api 層為 slot 2;層內依 id 字母序;weight medium、effort null
    assert deploy["slots"] == [{"slot": 2, "members": [
        {"id": "api-ab", "effort": None, "weight": "medium"},
        {"id": "api-b", "effort": None, "weight": "medium"},
        {"id": "api-z", "effort": None, "weight": "medium"}]}]


def _slots(route: dict) -> list[tuple[int, list[tuple[str, str, str | None]]]]:
    return [(s["slot"], [(m["id"], m["weight"], m["effort"]) for m in s["members"]]) for s in route["slots"]]


async def test_list_routes_follows_router_plan(seeded_gateway, fake_api, fake_cli):
    """終審修正 C(決策 12 修訂):兩種情形都走 `OrderPlanner.plan`——手動順序缺的成員接成後續層級、
    已不是成員的 target 略過、chat route 不列 cli 成員;與 Router 的 dry-run 順序相同。"""
    gw = seeded_gateway
    with gw.http() as http:
        r = http.post("/api/deployments", json={
            "id": "claude-sub", "model_name": "claude-sub", "kind": "cli", "provider": "claude-cli",
            "model": "claude-sonnet-5-5", "params": {"max_concurrency": 1, "timeout": 60},
            "capabilities": {"ctx": 1000000, "tools": False, "vision": False,
                             "efforts": ["low", "medium", "high", "max"]},
            "tags": ["a"]})
        assert r.status_code == 201, r.text
        for name in ("task-r", "task-d"):
            assert http.post("/api/routes", json={"name": name, "target": {"group": "coding"},
                                                  "mode": "task"}).status_code == 201
    with gw.db() as conn:
        repo.save_deployment(conn, api_member("api-c", fake_api.base_url, tags=("a",)))
        repo.save_deployment(conn, api_member("api-old", fake_api.base_url, tags=("a",)))
        manual = [repo.RouteTarget("api-ab", 1, 0, "low", "high"), repo.RouteTarget("api-old", 1, 1),
                  repo.RouteTarget("claude-sub", 3, 0, None, "low")]
        for name in ("review", "task-r"):
            repo.replace_route_targets(conn, name, manual)
        # api-old 已不是 coding 的成員:手動順序裡的它成為過時的 target
        repo.save_deployment(conn, api_member("api-old", fake_api.base_url, tags=("b",)))
    async with gw.mcp_session("coding") as c:
        r = await c.session.call_tool("list_routes", {})
    assert r.isError is False
    routes = {x["name"]: x for x in r.structuredContent["routes"]}
    assert {n: x["ordered"] for n, x in routes.items()} == {
        "review": "manual", "review-b": "default", "task-d": "default", "task-r": "manual"}
    rest = [("api-a", "medium", None), ("api-c", "medium", None)]
    # 手動 slot 1、3 → 未列入者從 slot 4 起依 4.3.2(cli 層 4 為空、api 層 5)
    assert _slots(routes["review"]) == [(1, [("api-ab", "high", "low")]), (5, rest)]   # chat:無 cli
    assert _slots(routes["task-r"]) == [(1, [("api-ab", "high", "low")]),
                                        (3, [("claude-sub", "low", None)]), (5, rest)]
    api_tier = [("api-a", "medium", None), ("api-ab", "medium", None), ("api-c", "medium", None)]
    assert _slots(routes["review-b"]) == [(2, api_tier)]                                  # chat:無 cli
    assert _slots(routes["task-d"]) == [(1, [("claude-sub", "medium", None)]), (2, api_tier)]

    # 與 Router 自己的順序(dry-run;slot 內依 position 排回)相同
    with gw.http() as http:
        for name, route in routes.items():
            plan = http.post(f"/api/routes/{name}/dry-run",
                             json={"messages": [{"role": "user", "content": "x"}]})
            assert plan.status_code == 200, plan.text
            router_slots = [
                (s["number"], [(e["deployment"], e["weight"]) for e in
                               sorted(s["entries"], key=lambda e: (e["position"], e["deployment"]))])
                for s in plan.json()["slots"]]
            assert router_slots == [(n, [(i, w) for i, w, _ in ms]) for n, ms in _slots(route)], name


# ---- quota_status ------------------------------------------------------------------------------

async def test_quota_status_scope_and_matches_dashboard(seeded_gateway, clock):
    gw = seeded_gateway
    with gw.db() as conn:
        from spillway.domain import Budget
        repo.save_budget(conn, "api-a", Budget(type="usd", limit_amount=10.0, window="monthly", reset_anchor=1))
        repo.mark_cooldown(conn, "api-b", clock.now().replace(year=clock.now().year + 1), "rate_limited")
    async with gw.mcp_session("coding") as c:
        await c.session.call_tool("ask", {"route": "review", "prompt": "spend"})
        coding = (await c.session.call_tool("quota_status", {})).structuredContent
    async with gw.mcp_session("all") as c:
        everyone = (await c.session.call_tool("quota_status", {})).structuredContent
    with gw.http() as http:
        dash = {d["id"]: d for d in http.get("/api/dashboard").json()["deployments"]}
        preview = [m["id"] for m in http.get("/api/groups/coding").json()["members"]]
    assert [m["id"] for m in coding["members"]] == preview == ["api-a", "api-ab"]
    assert [m["id"] for m in everyone["members"]] == sorted(dash) == ["api-a", "api-ab", "api-b"]
    for m in everyone["members"]:
        assert set(m) == {"id", "kind", "provider", "budget_type", "remaining", "window",
                          "cooling_until", "cooling_reason"}
        d = dash[m["id"]]
        for k in ("kind", "provider", "budget_type", "remaining", "window", "cooling_until", "cooling_reason"):
            assert m[k] == d[k], (m["id"], k)
    by_id = {m["id"]: m for m in everyone["members"]}
    assert by_id["api-a"]["budget_type"] == "usd"
    assert by_id["api-a"]["remaining"] == pytest.approx(10.0 - dash["api-a"]["period_spend"])
    assert sum(d["period_spend"] for d in dash.values()) > 0   # ask 確實花費
    assert by_id["api-b"]["cooling_reason"] == "rate_limited"


# ---- catalog 與 tools/list 同源 ---------------------------------------------------------------

@pytest.mark.parametrize("group", ["coding", "ops", "all"])
async def test_catalog_tools_equal_tools_list(seeded_gateway, group):
    from spillway.api.mcp import mcp_catalog

    gw = seeded_gateway
    async with gw.mcp_session(group) as c:
        listed = [t.model_dump(mode="json", by_alias=True, exclude_none=True)
                  for t in (await c.session.list_tools()).tools]
    cat = mcp_catalog(gw.app, group)
    assert cat["tools"] == listed
    assert cat["connection"]["url"] == gw.mcp_url(group)

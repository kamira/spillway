"""`mcp_catalog(app, group="all")`(CHG-20261102-01 T2;決策 8;AM-3、AM-13)。

`tools` 與 `tools/list` 逐欄相同的斷言在 `test_mcp_tools.py::test_catalog_tools_equal_tools_list`
(需要真實 SDK 用戶端)。這裡驗結構、errors 三類與決策 8 的清單一致、純資料無副作用。

註:design 錯誤碼表於 T9 依決策 8 改寫(三類);本檔以決策 8 的清單為準(`EXPECTED_*`),T9 同步 design
後兩者一致。
"""
from __future__ import annotations

import json

import pytest

from spillway.api.mcp import mcp_catalog
from spillway.store import repo

pytestmark = pytest.mark.anyio

# 決策 8(AM-13)
EXPECTED_HTTP = {"host_not_allowed": 403, "origin_not_allowed": 403, "recursion_blocked": 403,
                 "unauthorized": 401, "shutting_down": 503, "not_found": 404}
EXPECTED_TOOL = ["route_not_found", "no_candidate", "shutting_down", "internal_error", "validation_error"]
EXPECTED_SDK = [400, 404, 406, 413, 503]


async def test_catalog_structure(asgi_app, db):
    repo.save_group(db, repo.Group("coding", {"all": ["a"]}))
    repo.save_group(db, repo.Group('we"ird;$(x)', {"all": ["a"]}))
    repo.save_route(db, repo.Route("review", {"group": "coding"}))
    db.commit()
    app = asgi_app.app
    cat = mcp_catalog(app)
    assert set(cat) == {"group", "connection", "tools", "notifications", "errors"}
    assert cat["group"] == "all"
    conn = cat["connection"]
    assert conn["transport"] == "streamable-http"
    assert conn["url"] == "http://127.0.0.1:8787/mcp/all"
    assert [g["name"] for g in conn["groups"]] == ["all", "coding", 'we"ird;$(x)']
    assert conn["groups"][2]["url"] == "http://127.0.0.1:8787/mcp/we%22ird%3B%24%28x%29"
    assert conn["auth"]["type"] == "Bearer" and conn["auth"]["env"] == "SPILLWAY_TOKEN"
    assert conn["stdio"]["args"][:3] == ["mcp-stdio", "--group", "all"]
    assert [t["name"] for t in cat["tools"]] == ["ask", "list_routes", "quota_status"]
    assert [n["method"] for n in cat["notifications"]] == ["notifications/tools/list_changed"]
    errors = cat["errors"]
    assert set(errors) == {"http", "tool", "sdk_transport"}
    assert {e["code"]: e["status"] for e in errors["http"]} == EXPECTED_HTTP
    assert all(e["format"] == "spec-9.1.2" for e in errors["http"])
    assert [e["code"] for e in errors["tool"]] == EXPECTED_TOOL
    assert [e["status"] for e in errors["sdk_transport"]] == EXPECTED_SDK
    assert all(e["format"] in ("json-rpc", "text") for e in errors["sdk_transport"])
    assert asgi_app.token not in json.dumps(cat, ensure_ascii=False)
    json.dumps(cat)   # 可序列化


async def test_catalog_per_group(asgi_app, db):
    repo.save_group(db, repo.Group("coding", {"all": ["a"]}))
    repo.save_route(db, repo.Route("review", {"group": "coding"}))
    repo.save_route(db, repo.Route("other", {"tags": {"all": ["a"]}}))
    db.commit()
    coding = mcp_catalog(asgi_app.app, "coding")
    ask = coding["tools"][0]
    assert ask["inputSchema"]["properties"]["route"]["enum"] == ["review"]
    assert mcp_catalog(asgi_app.app)["tools"][0]["inputSchema"]["properties"]["route"]["enum"] == \
        ["other", "review"]
    assert mcp_catalog(asgi_app.app, "nope")["tools"] == []   # 不存在的群組:與 tools/list 相同


async def test_catalog_has_no_side_effects(asgi_app, db):
    before = [tuple(r) for r in db.execute("SELECT name FROM sqlite_master ORDER BY name")]
    counts = db.execute("SELECT (SELECT COUNT(*) FROM usage_ledger), (SELECT COUNT(*) FROM groups)").fetchone()
    mcp_catalog(asgi_app.app)
    mcp_catalog(asgi_app.app, "x")
    assert [tuple(r) for r in db.execute("SELECT name FROM sqlite_master ORDER BY name")] == before
    assert db.execute("SELECT (SELECT COUNT(*) FROM usage_ledger), (SELECT COUNT(*) FROM groups)").fetchone() == counts

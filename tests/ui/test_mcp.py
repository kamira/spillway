"""UI-5 MCP 設定(CHG-20261103-01 T7;決策 15,決策審議修訂 AM-12、AM-17;設計差異 #7)。

L2:5 種工具 ×(all、coding)顯示的 variants 數、路徑、內容與 `GET /api/mcp/config` 相同;「複製」後剪貼簿
內容等於該 variant 的 content;剪貼簿權限被拒時選取文字並顯示替代提示;「下載」的檔名等於 filename(去除
`/` 與 `\\`)、內容相等、Blob type 為 text/plain;charset=utf-8,點擊後 revoke;畫面不含 token。
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import pytest
from playwright.sync_api import Route, expect

from tests.helpers.live_gateway import LiveGateway
from tests.helpers.ui import MakeGw, NewPage, api, goto, guard_of, seed_group
from tests.helpers.ui_mcp import (BLOB_SPY, DENY_CLIPBOARD, TOOLS, mcp_config, mcp_variants, open_mcp,
                                  select_mcp)


ALLOWED_BLOB_TYPES = {"text/plain;charset=utf-8", "application/octet-stream"}


@pytest.fixture
def gw(make_ui_gateway: MakeGw) -> LiveGateway:
    g = make_ui_gateway()
    seed_group(g, "coding")
    return g


def _expected(cfg: dict) -> list[dict]:
    return [{"label": v["label"], "scope": v["scope"],
             "path": v["path"] if v["path"] is not None else "在終端機執行",
             "filename": v["filename"], "content": v["content"], "download": v["filename"] is not None}
            for v in cfg["variants"]]


def test_variants_match_api_for_every_tool_and_group(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_mcp(page, gw)
    expect(page.locator("#mcp-group option")).to_have_text(["all(全部 route)", "coding"])
    expect(page.locator("#mcp-tools [role=tab]")).to_have_count(len(TOOLS))
    for group in ("all", "coding"):
        for tool in TOOLS:
            select_mcp(page, group=group, tool=tool)
            cfg = mcp_config(gw, group, tool)
            assert mcp_variants(page) == _expected(cfg), (group, tool)
            expect(page.locator(f'#mcp-tools [data-tool="{tool}"]')).to_have_attribute("aria-selected", "true")
            expect(page.locator("#mcp-env [data-role=env]")).to_have_text(cfg["env"])
            expect(page.locator("#mcp-notes li")).to_have_count(len(cfg["notes"]))
            for i, note in enumerate(cfg["notes"]):
                expect(page.locator("#mcp-notes li").nth(i)).to_contain_text(note["text"])
            expect(page.locator("#mcp-endpoint")).to_have_text(gw.mcp_url(group))
    # 畫面不含 token(只有環境變數名)
    assert gw.token and gw.token not in page.content()
    expect(page.locator("main")).not_to_contain_text("內嵌 token")
    guard_of(page).assert_clean()


def test_query_selects_initial_group_and_tool(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_mcp(page, gw, group="coding", tool="gemini")
    expect(page.locator("#mcp-group")).to_have_value("coding")
    assert mcp_variants(page) == _expected(mcp_config(gw, "coding", "gemini"))


def test_copy_writes_variant_content_to_clipboard(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page(permissions=["clipboard-read", "clipboard-write"])
    open_mcp(page, gw, group="coding", tool="codex")
    cfg = mcp_config(gw, "coding", "codex")
    for i, v in enumerate(cfg["variants"]):
        page.locator(".mcp-variant").nth(i).locator("[data-role=copy]").click()
        expect(page.locator("#msgbar")).to_contain_text("已複製")
        assert page.evaluate("navigator.clipboard.readText()") == v["content"]
        expect(page.locator(".mcp-variant").nth(i).locator("[data-role=copy-hint]")).to_be_hidden()


def test_copy_denied_selects_text_and_shows_hint(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    page.add_init_script(script=DENY_CLIPBOARD)
    open_mcp(page, gw, tool="claude-code")
    content = mcp_config(gw, "all", "claude-code")["variants"][1]["content"]
    card = page.locator(".mcp-variant").nth(1)
    card.locator("[data-role=copy]").click()
    hint = card.locator("[data-role=copy-hint]")
    expect(hint).to_be_visible()
    expect(hint).to_contain_text("⌘C")
    expect(hint).to_contain_text("Ctrl+C")
    assert page.evaluate("window.getSelection().toString()") == content
    expect(page.locator("#msgbar")).to_be_hidden()


def test_download_uses_filename_and_plain_blob(new_page: NewPage, gw: LiveGateway, tmp_path: Path) -> None:
    page = new_page()
    page.add_init_script(script=BLOB_SPY)
    open_mcp(page, gw, group="coding", tool="cursor")
    cfg = mcp_config(gw, "coding", "cursor")
    for i, v in enumerate(cfg["variants"]):
        assert v["filename"]
        with page.expect_download() as info:
            page.locator(".mcp-variant").nth(i).locator("[data-role=download]").click()
        dl = info.value
        assert dl.suggested_filename == v["filename"]
        target = tmp_path / f"dl-{i}"
        dl.save_as(target)
        assert target.read_text(encoding="utf-8") == v["content"]
    blobs = page.evaluate("window.__blobs")
    assert len(blobs) == len(cfg["variants"])
    assert {b["type"] for b in blobs} <= ALLOWED_BLOB_TYPES
    # wait_for_function 以字串求值,會被 CSP 擋下;改為以 evaluate 輪詢(revoke 在點擊後的下一個 task)
    revoked: list[str] = []
    for _ in range(200):   # 上限約 2 秒
        revoked = page.evaluate("window.__revoked")
        if len(revoked) == len(blobs):
            break
        page.wait_for_timeout(10)
    assert sorted(revoked) == sorted(b["url"] for b in blobs)


def test_command_variant_has_no_download(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_mcp(page, gw, tool="claude-code")
    cards = page.locator(".mcp-variant")
    expect(cards).to_have_count(2)
    expect(cards.nth(0).locator("[data-role=download]")).to_have_count(1)
    expect(cards.nth(1).locator("[data-role=download]")).to_have_count(0)


def test_download_strips_slashes_from_filename(new_page: NewPage, gw: LiveGateway, tmp_path: Path) -> None:
    page = new_page()
    page.add_init_script(script=BLOB_SPY)

    def tamper(route: Route) -> None:
        resp = route.fetch()
        body = resp.json()
        body["variants"][0]["filename"] = "conf/ig\\mcp.json"
        route.fulfill(response=resp, body=json.dumps(body))

    page.route("**/api/mcp/config?*", tamper)
    open_mcp(page, gw, tool="cursor")
    card = page.locator(".mcp-variant").nth(0)
    expect(card.locator("[data-role=filename]")).to_have_text("configmcp.json")
    with page.expect_download() as info:
        card.locator("[data-role=download]").click()
    assert info.value.suggested_filename == "configmcp.json"
    assert {b["type"] for b in page.evaluate("window.__blobs")} <= ALLOWED_BLOB_TYPES


@pytest.mark.expect_http_errors(404)
def test_unknown_group_shows_error_as_text(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    name = '<img src=x onerror="window.__xss=1">'
    goto(page, gw, f"/ui/mcp.html?group={quote(name, safe='')}")
    expect(page.locator("#msgbar")).to_contain_text(name)
    expect(page.locator("main img")).to_have_count(0)
    assert page.evaluate("window.__xss") is None


@pytest.mark.expect_http_errors(404)
def test_failed_load_clears_previous_content(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_mcp(page, gw, group="coding", tool="gemini")
    expect(page.locator("#mcp-notes li")).not_to_have_count(0)
    api(gw, "DELETE", "/api/groups/coding", expect=(204,))
    # 群組已刪除(選單仍保留):先改選 all,再回 coding 觸發重新載入 → 404
    page.select_option("#mcp-group", "all")
    page.wait_for_selector('html[data-mcp-ready="all|gemini"]', state="attached")
    page.select_option("#mcp-group", "coding")
    expect(page.locator("html")).to_have_attribute("data-mcp-ready", "error")
    expect(page.locator("#msgbar")).to_be_visible()
    for box in ("#mcp-variants", "#mcp-env", "#mcp-notes", "#mcp-endpoint", "#mcp-toollist"):
        expect(page.locator(box)).to_be_empty()

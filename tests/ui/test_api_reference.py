"""UI-8 API 參考(CHG-20261103-01 T7;決策 4、19,決策審議修訂 AM-17;AC-22)。

L2:四大類標題依序為連線、工具、通知、錯誤碼;收合「工具」後其項目不可見,展開恢復;工具項目名稱集合等於
catalog 的 `tools[].name`;點選 `ask` 顯示說明與 inputSchema(與 catalog 的 JSON 相等)並更新網址 hash;網址
hash 直接開啟對應項目;切換群組後內容等於該群組的 catalog;以不存在且含 `<img onerror>` 的群組名開啟,錯誤以
文字顯示;畫面文字不含 `/api/` 與 `/v1`(連線類別中的 `/mcp/` URL 允許)。
"""
from __future__ import annotations

import json
from collections.abc import Callable
from urllib.parse import quote

import pytest
from playwright.sync_api import Page, expect

from tests.helpers.live_gateway import LiveGateway
from tests.helpers.ui import guard_of
from tests.helpers.ui_mcp import mcp_catalog, open_reference, seed_group

NewPage = Callable[..., Page]
MakeGw = Callable[..., LiveGateway]


@pytest.fixture
def gw(make_ui_gateway: MakeGw) -> LiveGateway:
    g = make_ui_gateway()
    seed_group(g, "coding")
    return g


def _detail(page: Page):  # noqa: ANN202
    return page.locator("#ref-detail")


def test_categories_in_order_and_collapsible(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_reference(page, gw)
    summaries = page.locator("#ref-index details > summary")
    expect(summaries).to_have_text(["連線", "工具", "通知", "錯誤碼"])
    for d in page.locator("#ref-index details").all():
        expect(d).to_have_attribute("open", "")
    tools = page.locator('#ref-index details[data-category="tools"] a')
    expect(tools.first).to_be_visible()
    page.locator('#ref-index details[data-category="tools"] > summary').click()
    for link in tools.all():
        expect(link).to_be_hidden()
    page.locator('#ref-index details[data-category="tools"] > summary').click()
    for link in tools.all():
        expect(link).to_be_visible()
    guard_of(page).assert_clean()


def test_index_items_match_catalog(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_reference(page, gw)
    cat = mcp_catalog(gw)
    names = page.locator('#ref-index details[data-category="tools"] a').all_text_contents()
    assert set(names) == {t["name"] for t in cat["tools"]}
    assert len(names) == len(cat["tools"])
    expect(page.locator('#ref-index details[data-category="notifications"] a')).to_have_text(
        [n["method"] for n in cat["notifications"]])
    expect(page.locator('#ref-index details[data-category="connection"] a')).to_have_text(
        ["端點 URL", "驗證方式", "mcp-stdio"])
    expect(page.locator('#ref-index details[data-category="errors"] a')).to_have_text(["HTTP", "工具", "SDK 傳輸"])


def test_click_ask_shows_description_and_schema(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_reference(page, gw)
    ask = next(t for t in mcp_catalog(gw)["tools"] if t["name"] == "ask")
    page.locator('#ref-index a[data-item="tool-ask"]').click()
    expect(page).to_have_url(gw.url("/ui/api-reference.html#tool-ask"))
    expect(_detail(page)).to_have_attribute("data-item", "tool-ask")
    expect(page.locator('#ref-index a[data-item="tool-ask"]')).to_have_attribute("aria-current", "true")
    expect(_detail(page).locator("[data-role=description]")).to_have_text(ask["description"])
    assert json.loads(_detail(page).locator("[data-role=input-schema]").text_content()) == ask["inputSchema"]
    assert json.loads(_detail(page).locator("[data-role=output-schema]").text_content()) == ask["outputSchema"]


@pytest.mark.parametrize("hash_,heading", [
    ("#tool-quota_status", "quota_status"),
    ("#conn-auth", "驗證方式"),
    ("#errors-sdk", "錯誤碼:SDK 傳輸"),
    ("#" + quote("notify-notifications/tools/list_changed", safe=""), "notifications/tools/list_changed"),
])
def test_hash_deep_link_opens_item(new_page: NewPage, gw: LiveGateway, hash_: str, heading: str) -> None:
    page = new_page()
    open_reference(page, gw, hash_=hash_)
    expect(_detail(page).locator("h2").first).to_have_text(heading)


def test_hash_change_and_back_navigation(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_reference(page, gw, hash_="#tool-list_routes")
    expect(_detail(page)).to_have_attribute("data-item", "tool-list_routes")
    page.locator('#ref-index a[data-item="errors-http"]').click()
    expect(_detail(page)).to_have_attribute("data-item", "errors-http")
    codes = _detail(page).locator("tbody tr td:first-child").all_text_contents()
    assert codes == [e["code"] for e in mcp_catalog(gw)["errors"]["http"]]
    page.go_back()
    expect(_detail(page)).to_have_attribute("data-item", "tool-list_routes")


def test_switch_group_shows_that_groups_catalog(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_reference(page, gw, hash_="#tool-ask")
    expect(page.locator("#ref-group option")).to_have_text(["all(全部 route)", "coding"])
    page.select_option("#ref-group", "coding")
    page.wait_for_selector('html[data-ref-ready="coding"]', state="attached")
    cat = mcp_catalog(gw, "coding")
    ask = next(t for t in cat["tools"] if t["name"] == "ask")
    expect(_detail(page).locator("[data-role=description]")).to_have_text(ask["description"])
    assert json.loads(_detail(page).locator("[data-role=input-schema]").text_content()) == ask["inputSchema"]
    expect(page).to_have_url(gw.url("/ui/api-reference.html?group=coding#tool-ask"))
    page.locator('#ref-index a[data-item="conn-endpoint"]').click()
    expect(_detail(page).locator("dd").first).to_have_text(cat["connection"]["url"])
    rows = _detail(page).locator("tbody tr")
    expect(rows).to_have_count(len(cat["connection"]["groups"]))
    page.locator('#ref-index a[data-item="conn-stdio"]').click()
    expect(_detail(page)).to_contain_text(" ".join([cat["connection"]["stdio"]["command"],
                                                    *cat["connection"]["stdio"]["args"]]))


def test_group_query_opens_that_group(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_reference(page, gw, query="?group=coding", hash_="#conn-endpoint")
    expect(page.locator("#ref-group")).to_have_value("coding")
    expect(_detail(page).locator("dd").first).to_have_text(gw.mcp_url("coding"))


@pytest.mark.expect_http_errors(404)
def test_unknown_group_with_markup_shows_error_as_text(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    name = '<img src=x onerror="window.__xss=1">'
    open_reference(page, gw, query=f"?group={quote(name, safe='')}")
    expect(page.locator("html")).to_have_attribute("data-ref-ready", "error")
    expect(page.locator("#msgbar")).to_be_visible()
    expect(page.locator("#msgbar")).to_contain_text(name)
    expect(page.locator("main img")).to_have_count(0)
    assert page.evaluate("window.__xss") is None
    expect(page.locator("#ref-index a")).to_have_count(0)


def test_page_text_has_no_admin_or_v1_paths(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    for group in ("all", "coding"):
        open_reference(page, gw, query=f"?group={group}")
        ids = [a.get_attribute("data-item") for a in page.locator("#ref-index a[data-item]").all()]
        assert len(ids) == 3 + len(mcp_catalog(gw, group)["tools"]) + 1 + 3
        for item in ids:
            page.locator(f'#ref-index a[data-item="{item}"]').click()
            expect(_detail(page)).to_have_attribute("data-item", item)
            text = page.locator("body").inner_text()
            assert "/api/" not in text, item
            assert "/v1" not in text, item
        # 連線類別中的 /mcp/ URL 允許(而且要出現)
        page.locator('#ref-index a[data-item="conn-endpoint"]').click()
        expect(_detail(page)).to_contain_text("/mcp/")
    assert gw.token and gw.token not in page.content()
    guard_of(page).assert_clean()

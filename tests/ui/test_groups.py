"""UI-3 群組與 Tag(CHG-20261103-01 T5;決策 13、24)。

L2:選擇器三列(all / any / none)加入、移除 tag 後,畫面在 300 ms 防抖後以 `POST /api/groups/preview`
更新預覽,預覽成員集合等於該端點的回應;新增 `..` → 422 訊息;新增與編輯寫入;刪除被 route 引用的群組 →
409 訊息含 route 名;「所有 Tag」列出全部 tag 與成員,點成員開啟 UI-2 編輯表單。
"""
from __future__ import annotations


import pytest
from playwright.sync_api import Page, expect

from tests.helpers.live_gateway import LiveGateway
from tests.helpers.ui import MakeGw, NewPage, api, guard_of, seed_group
from tests.helpers.ui_members import MemberForm, open_groups, requests_of, seed_member, seed_route


@pytest.fixture
def gw(make_ui_gateway: MakeGw) -> LiveGateway:
    g = make_ui_gateway()
    seed_member(g, "a1", tags=["coding", "cheap"])
    seed_member(g, "a2", tags=["coding", "review"])
    seed_member(g, "a3", tags=["review"], enabled=False)
    seed_member(g, "a4", tags=["private-ok", "coding"], kind="cli", provider="claude-cli",
                model="claude-sonnet-5-5", params={}, credential_ref=None)
    return g


def preview_ids(page: Page) -> list[str]:
    return page.locator('#group-editor [data-role="preview"] li[data-member]').evaluate_all(
        "(ls) => ls.map((l) => l.getAttribute('data-member'))")


def api_preview(gw: LiveGateway, selector: dict) -> list[str]:
    return [m["id"] for m in api(gw, "POST", "/api/groups/preview", {"selector": selector}).json()["members"]]


def add(page: Page, row: str, tag: str) -> None:
    box = page.locator(f'#group-editor input[data-tag-input="{row}"]')
    box.fill(tag)
    box.press("Enter")


def expect_preview(page: Page, ids: list[str]) -> None:
    page.wait_for_function(
        """(ids) => JSON.stringify([...document.querySelectorAll('#group-editor [data-role="preview"] li[data-member]')]
             .map((l) => l.getAttribute('data-member'))) === JSON.stringify(ids)""", arg=ids)
    assert preview_ids(page) == ids
    expect(page.locator('#group-editor [data-role="preview-title"]')).to_have_text(f"符合的成員({len(ids)})")


def test_selector_rows_live_preview(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_groups(page, gw)
    page.locator("#new-group").click()
    expect_preview(page, api_preview(gw, {}))          # 空條件符合全部成員
    guard = guard_of(page)
    sel: dict[str, list[str]] = {"all": [], "any": [], "none": []}
    for row, tag in (("all", "coding"), ("any", "cheap"), ("any", "review"), ("none", "private-ok")):
        before = len(requests_of(guard, "POST", "/api/groups/preview"))
        sel[row].append(tag)
        with page.expect_response(lambda r: r.request.method == "POST" and "/api/groups/preview" in r.url) as resp:
            add(page, row, tag)
        expect(page.locator(f'#group-editor [data-chips="{row}"] [data-tag="{tag}"]')).to_be_visible()
        expected = api_preview(gw, sel)
        assert [m["id"] for m in resp.value.json()["members"]] == expected
        assert resp.value.request.post_data_json == {"selector": sel}
        expect_preview(page, expected)
        assert len(requests_of(guard, "POST", "/api/groups/preview")) == before + 1
    assert preview_ids(page) == ["a1", "a2"]
    # × 移除 → 預覽更新
    sel["any"] = ["cheap"]
    with page.expect_response(lambda r: "/api/groups/preview" in r.url) as resp:
        page.locator('#group-editor button[aria-label="移除 any review"]').click()
    assert resp.value.request.post_data_json == {"selector": sel}
    expect_preview(page, api_preview(gw, sel))
    assert preview_ids(page) == ["a1"]
    # 停用成員也列出並標示
    sel["all"], sel["any"], sel["none"] = [], ["review"], []
    page.locator('#group-editor button[aria-label="移除 all coding"]').click()
    page.locator('#group-editor button[aria-label="移除 none private-ok"]').click()
    page.locator('#group-editor button[aria-label="移除 any cheap"]').click()
    add(page, "any", "review")
    expect_preview(page, api_preview(gw, sel))
    assert preview_ids(page) == ["a2", "a3"]
    expect(page.locator('#group-editor [data-role="preview"] li[data-member="a3"]')).to_contain_text("停用")


def test_preview_is_debounced(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_groups(page, gw)
    page.locator("#new-group").click()
    expect_preview(page, api_preview(gw, {}))
    guard = guard_of(page)
    before = len(requests_of(guard, "POST", "/api/groups/preview"))
    # 一次同步加入三個 tag(同一個 task 內,間隔遠小於 300 ms)→ 只送出一次預覽
    page.evaluate("""() => {
        const box = document.querySelector('#group-editor input[data-tag-input="any"]');
        for (const t of ["cheap", "review", "nope"]) {
            box.value = t;
            box.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true}));
        }
    }""")
    expect_preview(page, api_preview(gw, {"any": ["cheap", "review", "nope"]}))
    with page.expect_request(lambda r: r.method == "POST" and "/api/groups/preview" in r.url):
        add(page, "none", "x")      # 確認之後的請求照常送出,且前面那批只算一次
    assert len(requests_of(guard, "POST", "/api/groups/preview")) == before + 2


@pytest.mark.expect_http_errors(422)
def test_create_group_and_422(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_groups(page, gw)
    page.locator("#new-group").click()
    page.locator("#group-name").fill("..")
    add(page, "all", "coding")
    page.locator('#group-editor [data-role="save"]').click()
    expect(page.locator("#msgbar")).to_contain_text("不可為 . 或 ..")
    assert api(gw, "GET", "/api/groups").json()["groups"] == []
    page.locator("#group-name").fill("coding")
    page.locator('#group-editor [data-role="save"]').click()
    expect(page.locator("#msgbar")).to_contain_text("已儲存群組 coding")
    item = page.locator('#group-list button[data-group="coding"]')
    expect(item).to_contain_text("3 個成員")
    expect(item).to_have_attribute("aria-current", "true")
    g = api(gw, "GET", "/api/groups/coding").json()
    assert g["selector"] == {"all": ["coding"], "any": [], "none": []}
    # 編輯既有群組:名稱唯讀,PUT 選擇器
    expect(page.locator("#group-name")).to_be_disabled()
    add(page, "none", "cheap")
    with page.expect_request(lambda r: r.method == "PUT" and r.url.endswith("/api/groups/coding")):
        page.locator('#group-editor [data-role="save"]').click()
    expect(item).to_contain_text("2 個成員")
    assert api(gw, "GET", "/api/groups/coding").json()["selector"] == {"all": ["coding"], "any": [],
                                                                       "none": ["cheap"]}
    expect(page.locator('#group-editor [data-role="mcp"]')).to_contain_text("/mcp/coding")


@pytest.mark.expect_http_errors(409)
def test_delete_referenced_group_409(new_page: NewPage, gw: LiveGateway) -> None:
    seed_group(gw, "coding", {"all": ["coding"]})
    seed_group(gw, "spare", {"any": ["review"]})
    seed_route(gw, "review", {"group": "coding"})
    page = new_page()
    open_groups(page, gw)
    page.locator('#group-list button[data-group="coding"]').click()
    expect(page.locator('#group-editor [data-role="routes"]')).to_contain_text("review")
    page.locator('#group-editor [data-role="delete"]').click()
    page.locator('dialog [data-role="confirm"]').click()
    expect(page.locator("#msgbar")).to_contain_text("仍被 route 參照:review")
    assert api(gw, "GET", "/api/groups/coding").status_code == 200
    # 取消不刪;確認後刪除未被引用的群組
    page.locator('#group-list button[data-group="spare"]').click()
    page.locator('#group-editor [data-role="delete"]').click()
    page.locator('dialog [data-role="cancel"]').click()
    assert [g["name"] for g in api(gw, "GET", "/api/groups").json()["groups"]] == ["coding", "spare"]
    page.locator('#group-editor [data-role="delete"]').click()
    page.locator('dialog [data-role="confirm"]').click()
    expect(page.locator("#msgbar")).to_contain_text("已刪除群組 spare")
    expect(page.locator('#group-list button[data-group="spare"]')).to_have_count(0)
    assert [g["name"] for g in api(gw, "GET", "/api/groups").json()["groups"]] == ["coding"]


def test_tag_list_links_to_member_editor(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_groups(page, gw)
    deps = api(gw, "GET", "/api/deployments").json()["deployments"]
    tags = sorted({t for d in deps for t in d["tags"]})
    items = page.locator("#tag-list li[data-tag]")
    expect(items).to_have_count(len(tags))
    assert items.evaluate_all("(ls) => ls.map((l) => l.getAttribute('data-tag'))") == tags
    for t in tags:
        members = [d["id"] for d in deps if t in d["tags"]]
        links = page.locator(f'#tag-list li[data-tag="{t}"] a[data-member]')
        assert links.evaluate_all("(as) => as.map((a) => a.textContent)") == members
    expect(page.locator("#tag-card")).not_to_contain_text("改名")
    page.locator('#tag-list li[data-tag="review"] a[data-member="a3"]').click()
    expect(page).to_have_url(gw.url("/ui/members.html#a3"))
    expect(page.locator("#editor-title")).to_have_text("編輯 a3")
    form = MemberForm(page)
    form.wait_ready()
    expect(form.field("id")).to_have_value("a3")


@pytest.mark.expect_http_errors(422)
def test_preview_error_clears_stale_result(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_groups(page, gw)
    page.locator("#new-group").click()
    with page.expect_response(lambda r: "/api/groups/preview" in r.url):
        add(page, "all", "cheap")
    expect_preview(page, ["a1"])
    with page.expect_response(lambda r: "/api/groups/preview" in r.url) as resp:
        add(page, "any", "x" * 201)
    assert resp.value.status == 422
    preview = page.locator('#group-editor [data-role="preview"]')
    expect(preview).to_have_attribute("data-stale", "true")
    expect(preview.locator("li")).to_have_count(0)
    expect(page.locator('#group-editor [data-role="preview-title"]')).to_have_text("預覽失敗")
    expect(page.locator("#msgbar")).to_have_class("msgbar msg-error")
    # 移除過長的 tag 後恢復
    page.locator(f'#group-editor button[aria-label="移除 any {"x" * 201}"]').click()
    expect_preview(page, ["a1"])
    expect(preview).not_to_have_attribute("data-stale", "true")


def test_preview_debounce_300ms_clock(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    page.clock.install()
    open_groups(page, gw)
    page.locator("#new-group").click()
    expect_preview(page, api_preview(gw, {}))
    guard = guard_of(page)
    before = len(requests_of(guard, "POST", "/api/groups/preview"))
    add(page, "any", "cheap")
    add(page, "any", "review")
    page.clock.fast_forward(299)
    page.wait_for_function("() => true")
    assert len(requests_of(guard, "POST", "/api/groups/preview")) == before
    with page.expect_request(lambda r: r.method == "POST" and "/api/groups/preview" in r.url) as req:
        page.clock.fast_forward(1)
    assert req.value.post_data_json == {"selector": {"all": [], "any": ["cheap", "review"], "none": []}}
    expect_preview(page, api_preview(gw, {"any": ["cheap", "review"]}))
    assert len(requests_of(guard, "POST", "/api/groups/preview")) == before + 1


def test_group_names_and_tags_rendered_as_text(new_page: NewPage, gw: LiveGateway) -> None:
    evil = '<img src=x onerror="window.__xss=1">'
    seed_member(gw, "e1", tags=[evil])
    seed_group(gw, "<b>g", {"any": [evil]})
    page = new_page()
    open_groups(page, gw)
    expect(page.locator('#group-list button[data-group="<b>g"]')).to_contain_text("<b>g")
    expect(page.locator("#tag-list")).to_contain_text(evil)
    page.locator('#group-list button[data-group="<b>g"]').click()
    expect(page.locator('#group-editor [data-chips="any"]')).to_contain_text(evil)
    assert page.locator("main img, main b").count() == 0
    assert page.evaluate("() => window.__xss") is None


# ---- 終審修正(CHG-20261103-01 終審必修 1、2) ------------------------------------------------------

def test_save_commits_pending_tag_input(new_page: NewPage, gw: LiveGateway) -> None:
    """輸入但沒按 Enter 的 tag,按儲存時一併送出(新群組與既有群組各一次)。"""
    page = new_page()
    open_groups(page, gw)
    page.locator("#new-group").click()
    page.locator("#group-name").fill("pending")
    page.locator('#group-editor input[data-tag-input="none"]').fill("x")
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/api/groups")) as req:
        page.locator('#group-editor [data-role="save"]').click()
    assert req.value.post_data_json["selector"]["none"] == ["x"]
    expect(page.locator("#msgbar")).to_contain_text("已儲存群組 pending")
    assert api(gw, "GET", "/api/groups/pending").json()["selector"]["none"] == ["x"]
    # 既有群組:編輯器重開後,再輸入一個未按 Enter 的 tag
    expect(page.locator('#group-editor [data-chips="none"] [data-tag="x"]')).to_be_visible()
    page.locator('#group-editor input[data-tag-input="all"]').fill("coding")
    with page.expect_request(lambda r: r.method == "PUT" and r.url.endswith("/api/groups/pending")):
        page.locator('#group-editor [data-role="save"]').click()
    expect(page.locator('#group-editor [data-chips="all"] [data-tag="coding"]')).to_be_visible()
    assert api(gw, "GET", "/api/groups/pending").json()["selector"] == {"all": ["coding"], "any": [],
                                                                        "none": ["x"]}


def test_mcp_link_carries_group(new_page: NewPage, gw: LiveGateway) -> None:
    name = "a&b#c?"
    seed_group(gw, name, {"all": ["coding"]})
    page = new_page()
    open_groups(page, gw)
    page.locator('#group-list button[data-group="a&b#c?"]').click()
    link = page.locator('#group-editor [data-role="mcp"] a')
    expect(link).to_have_attribute("href", "/ui/mcp.html?group=a%26b%23c%3F")
    link.click()
    page.wait_for_url("**/ui/mcp.html?group=a%26b%23c%3F")
    page.wait_for_selector('html[data-shell="ready"]', state="attached")
    expect(page.locator("#mcp-group")).to_have_value(name)

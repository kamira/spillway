"""UI-4 Route 清單與編輯頁(CHG-20261103-01 T6;決策 14,決策審議修訂 AM-1、AM-9、AM-10、AM-14、AM-17)。

L1:拖曳狀態轉換 `applyMove(slots, move)` 為純函式,以 `page.evaluate` 逐案驗證。
L2:編輯頁初始 slot 等於 `effective-order`;頁首「順序來源」、權重說明、chat 註記;HTML5 拖曳(卡片到既有
slot、到新 slot 區、slot 整列)與鍵盤替代得到相同結果;空 slot 自動移除;未儲存標示與 `beforeunload`;
儲存 body 等於畫面狀態並重新讀取;停用、冷卻、未排序卡片的標示與保留;stale 成員不出現且儲存後清掉;
`slots=[]` 走還原預設確認;還原預設;effort/權重選單;slot 的類型標籤與剩餘額度;dry-run 面板;
`member_not_in_target` 422;清單頁(tags、新增、刪除、記錄 context 三態)。
"""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import Page, expect

from tests.helpers.live_gateway import LiveGateway
from tests.helpers.ui import api, goto, guard_of, import_module
from tests.helpers.ui_routes import (
    add_members, api_body, card, create_route, drag_card, drag_slot, editor_slots, effective_slots,
    local_body, new_slot_zone, open_editor, put_order, route_order, route_order_full, set_cooldown,
    set_enabled, slot, slot_head,
)

NewPage = Callable[..., Page]
MakeGw = Callable[..., LiveGateway]


@pytest.fixture
def gw(make_ui_gateway: MakeGw, fake_api) -> LiveGateway:  # noqa: ANN001
    g = make_ui_gateway()
    body_a = api_body("api-a", fake_api)
    body_a["budget"] = {"type": "usd", "limit_amount": 10, "window": "monthly"}
    add_members(g, body_a, api_body("api-b", fake_api), local_body("q-local", fake_api),
                api_body("x-other", fake_api, tags=["other"]))
    create_route(g, "rt", {"tags": {"all": ["r"]}}, mode="task")
    return g


@pytest.fixture
def page(new_page: NewPage) -> Page:
    return new_page()


def route_targets(gw: LiveGateway, name: str) -> list[str]:
    with gw.db() as conn:
        return sorted(r[0] for r in conn.execute(
            "SELECT deployment_id FROM route_targets WHERE route_name = ?", (name,)))


# ---- L1:applyMove ------------------------------------------------------------------------------

def test_apply_move_pure(page: Page, gw: LiveGateway) -> None:
    open_editor(page, gw, "rt")
    import_module(page, "route-editor.js")
    cases = page.evaluate("""() => {
        const { applyMove, orderKey, orderBody } = window.__mods["route-editor.js"];
        const e = (id, extra = {}) => ({ deployment: id, effort: null, weight: "medium", unsorted: false, ...extra });
        const base = [{ entries: [e("a"), e("b")] }, { entries: [e("c", { unsorted: true })] }, { entries: [e("d")] }];
        const frozen = JSON.stringify(base);
        const ids = (s) => s.map((x) => x.entries.map((y) => y.deployment));
        const out = {};
        out.toExisting = ids(applyMove(base, { kind: "card", deployment: "a", to: 2 }));
        out.toIndex = ids(applyMove(base, { kind: "card", deployment: "d", to: 0, index: 1 }));
        out.toNew = ids(applyMove(base, { kind: "card", deployment: "b", to: "new" }));
        out.emptyRemoved = ids(applyMove(base, { kind: "card", deployment: "c", to: 0 }));
        out.unsortedCleared = applyMove(base, { kind: "card", deployment: "c", to: 2 })[1].entries[1].unsorted;
        out.slotUp = ids(applyMove(base, { kind: "slot", from: 2, to: 0 }));
        out.slotDown = ids(applyMove(base, { kind: "slot", from: 0, to: 3 }));
        out.slotMid = ids(applyMove(base, { kind: "slot", from: 0, to: 2 }));
        out.up = ids(applyMove(base, { kind: "up", index: 1 }));
        out.down = ids(applyMove(base, { kind: "down", index: 1 }));
        out.upFirst = ids(applyMove(base, { kind: "up", index: 0 }));
        out.downLast = ids(applyMove(base, { kind: "down", index: 2 }));
        out.badSlot = ids(applyMove(base, { kind: "card", deployment: "a", to: 9 }));
        out.badCard = ids(applyMove(base, { kind: "card", deployment: "zz", to: 0 }));
        const set = applyMove(base, { kind: "set", deployment: "b", effort: "high", weight: "low" });
        out.set = [set[0].entries[1].effort, set[0].entries[1].weight];
        out.unset = applyMove(set, { kind: "set", deployment: "b", effort: null })[0].entries[1].effort;
        out.allOut = ids(applyMove([{ entries: [e("a")] }], { kind: "card", deployment: "a", to: "new" }));
        out.untouched = JSON.stringify(base) === frozen;
        out.sameRefs = applyMove(base, { kind: "up", index: 0 })[0] !== base[0];
        out.keyWeight = orderKey(base) !== orderKey(set);
        out.keyUnsortedIgnored = orderKey(base) === orderKey(applyMove(base, { kind: "card", deployment: "c", to: 1 }));
        out.body = orderBody(set);
        try { applyMove(base, { kind: "nope" }); out.unknown = "no error"; } catch (err) { out.unknown = err.name; }
        return out;
    }""")
    assert cases["toExisting"] == [["b"], ["c"], ["d", "a"]]
    assert cases["toIndex"] == [["a", "d", "b"], ["c"]]
    assert cases["toNew"] == [["a"], ["c"], ["d"], ["b"]]
    assert cases["emptyRemoved"] == [["a", "b", "c"], ["d"]]
    assert cases["unsortedCleared"] is False
    assert cases["slotUp"] == [["d"], ["a", "b"], ["c"]]
    assert cases["slotDown"] == [["c"], ["d"], ["a", "b"]]
    assert cases["slotMid"] == [["c"], ["a", "b"], ["d"]]
    assert cases["up"] == [["c"], ["a", "b"], ["d"]]
    assert cases["down"] == [["a", "b"], ["d"], ["c"]]
    assert cases["upFirst"] == cases["downLast"] == cases["badSlot"] == cases["badCard"] == [["a", "b"], ["c"], ["d"]]
    assert cases["set"] == ["high", "low"]
    assert cases["unset"] is None
    assert cases["allOut"] == [["a"]]
    assert cases["untouched"] and cases["sameRefs"] and cases["keyWeight"] and cases["keyUnsortedIgnored"]
    assert cases["body"] == {"slots": [
        [{"deployment": "a", "effort": None, "weight": "medium"}, {"deployment": "b", "effort": "high", "weight": "low"}],
        [{"deployment": "c", "effort": None, "weight": "medium"}],
        [{"deployment": "d", "effort": None, "weight": "medium"}]]}
    assert cases["unknown"] == "TypeError"


# ---- L2:編輯頁 ---------------------------------------------------------------------------------

def test_editor_initial_state_and_header(page: Page, gw: LiveGateway) -> None:
    open_editor(page, gw, "rt")
    assert editor_slots(page) == effective_slots(gw, "rt") == [["api-a", "api-b"], ["q-local"]]
    expect(page.locator("#route-name")).to_have_text("rt")
    expect(page.locator("#order-source")).to_have_text("預設")
    expect(page.locator("#weight-note")).to_have_text("權重:高 3/中 2/低 1")
    expect(page.locator("#dry-random")).to_have_text("slot 內依權重隨機抽選,每次順序可能不同")
    expect(page.locator("#chat-note")).to_be_hidden()
    expect(page.locator("#dirty-banner")).to_be_hidden()
    # 每個 slot 顯示成員類型標籤與剩餘額度(/api/dashboard)
    expect(card(page, "api-a").locator(".badge.b-api")).to_have_text("API")
    expect(card(page, "api-a").locator('[data-role="remaining"]')).to_have_text("剩餘 $10.00")
    expect(card(page, "api-b").locator('[data-role="remaining"]')).to_have_text("剩餘 不限")
    expect(card(page, "q-local").locator(".badge.b-local")).to_have_text("Local")
    expect(slot(page, 1).locator(".slot-summary .badge")).to_have_text(["Local"])
    # effort 選單只列 capabilities.efforts 與「不指定」;權重為高、中、低
    assert card(page, "api-a").locator('select[data-role="effort"] option').all_inner_texts() == \
        ["不指定", "low", "medium", "high"]
    assert card(page, "q-local").locator('select[data-role="effort"] option').all_inner_texts() == ["不指定"]
    assert card(page, "api-a").locator('select[data-role="weight"] option').all_inner_texts() == ["高", "中", "低"]
    # 「移到…」選單:slot 1…N 與新 slot
    assert card(page, "api-a").locator('select[data-role="move"] option').all_inner_texts() == \
        ["移到…", "Slot 1", "Slot 2", "新 slot"]
    # 手動順序存在時標「手動」;chat route 頁首註明不使用 CLI 成員
    put_order(gw, "rt", [["api-b"], ["api-a"]])
    create_route(gw, "chatty", {"tags": {"all": ["r"]}}, mode="chat")
    open_editor(page, gw, "rt")
    expect(page.locator("#order-source")).to_have_text("手動")
    open_editor(page, gw, "chatty")
    expect(page.locator("#chat-note")).to_have_text("chat route 不使用 CLI 成員")
    expect(page.locator("#chat-note")).to_be_visible()


def test_drag_and_drop_then_save(page: Page, gw: LiveGateway) -> None:
    open_editor(page, gw, "rt")
    # 拖曳目標(各 slot 與新 slot 區)在 dragstart 之前就可見且有尺寸
    expect(new_slot_zone(page)).to_be_visible()
    assert new_slot_zone(page).bounding_box()["height"] > 0
    drag_card(page, "api-b", "new")
    assert editor_slots(page) == [["api-a"], ["q-local"], ["api-b"]]
    expect(page.locator("#dirty-banner")).to_be_visible()
    expect(page.locator("#dirty-banner")).to_contain_text("有未儲存的變更")
    drag_slot(page, 2, 0)                       # slot 3 整列拖到 slot 1 之前
    assert editor_slots(page) == [["api-b"], ["api-a"], ["q-local"]]
    drag_slot(page, 0, 2)                       # 往下拖:放到目標 slot 之後
    assert editor_slots(page) == [["api-a"], ["q-local"], ["api-b"]]
    drag_slot(page, 2, 0)
    drag_card(page, "api-a", 0)                 # 拖到既有 slot;空的 slot 自動移除
    assert editor_slots(page) == [["api-b", "api-a"], ["q-local"]]
    card(page, "api-a").locator('select[data-role="effort"]').select_option("high")
    card(page, "api-a").locator('select[data-role="weight"]').select_option("high")
    with page.expect_request(lambda r: r.method == "PUT" and r.url.endswith("/api/routes/rt/order")) as req, \
            page.expect_response(lambda r: r.url.endswith("/api/routes/rt/effective-order")) as reread:
        page.locator("#btn-save").click()
    assert req.value.post_data_json == {"slots": [
        [{"deployment": "api-b", "effort": None, "weight": "medium"},
         {"deployment": "api-a", "effort": "high", "weight": "high"}],
        [{"deployment": "q-local", "effort": None, "weight": "medium"}]]}
    assert reread.value.status == 200
    expect(page.locator("#dirty-banner")).to_be_hidden()
    expect(page.locator("#order-source")).to_have_text("手動")
    assert route_order_full(gw, "rt") == [
        [{"deployment": "api-b", "effort": None, "weight": "medium"},
         {"deployment": "api-a", "effort": "high", "weight": "high"}],
        [{"deployment": "q-local", "effort": None, "weight": "medium"}]]
    assert editor_slots(page) == effective_slots(gw, "rt") == route_order(gw, "rt")
    expect(card(page, "api-a").locator('select[data-role="effort"]')).to_have_value("high")


def test_keyboard_alternatives_match_drag(page: Page, gw: LiveGateway) -> None:
    open_editor(page, gw, "rt")
    card(page, "api-b").locator('select[data-role="move"]').select_option("new")
    assert editor_slots(page) == [["api-a"], ["q-local"], ["api-b"]]
    expect(page.locator("#dirty-banner")).to_be_visible()
    expect(slot_head(page, 0).locator('[data-role="up"]')).to_be_disabled()
    expect(slot_head(page, 2).locator('[data-role="down"]')).to_be_disabled()
    slot_head(page, 2).locator('[data-role="up"]').click()
    slot_head(page, 1).locator('[data-role="up"]').click()
    assert editor_slots(page) == [["api-b"], ["api-a"], ["q-local"]]   # 同拖曳 drag_slot(2, 0)
    slot_head(page, 0).locator('[data-role="down"]').click()
    slot_head(page, 1).locator('[data-role="down"]').click()
    assert editor_slots(page) == [["api-a"], ["q-local"], ["api-b"]]   # 同拖曳 drag_slot(0, 2)
    slot_head(page, 2).get_by_role("button", name="Slot 3 上移").click()
    slot_head(page, 1).get_by_role("button", name="Slot 2 上移").click()
    card(page, "api-a").locator('select[data-role="move"]').select_option(label="Slot 1")
    assert editor_slots(page) == [["api-b", "api-a"], ["q-local"]]     # 與 test_drag_and_drop_then_save 相同
    # 改回原狀時不再標示未儲存
    card(page, "api-b").locator('select[data-role="move"]').select_option("new")
    slot_head(page, 2).locator('[data-role="up"]').click()
    card(page, "api-b").locator('select[data-role="move"]').select_option(label="Slot 1")
    assert editor_slots(page) == [["api-a", "api-b"], ["q-local"]]
    expect(page.locator("#dirty-banner")).to_be_hidden()


def test_beforeunload_prompts_when_dirty(page: Page, gw: LiveGateway) -> None:
    open_editor(page, gw, "rt")
    slot_head(page, 1).locator('[data-role="up"]').click()      # 以 click 製造變更
    expect(page.locator("#dirty-banner")).to_be_visible()
    guard = guard_of(page)
    seen: list[str] = []

    def accept(dialog) -> None:  # noqa: ANN001
        seen.append(dialog.type)
        dialog.accept()

    with guard.manual_dialogs():
        page.once("dialog", accept)
        goto(page, gw, "/ui/routes.html")
    assert seen == ["beforeunload"]
    expect(page.locator("main h1")).to_have_text("Route")
    # 沒有變更時離開不提示
    open_editor(page, gw, "rt")
    page.locator("main h1").click()
    before = len(guard.dialogs)
    goto(page, gw, "/ui/routes.html")
    assert len(guard.dialogs) == before


def test_disabled_cooling_unsorted_cards_are_kept(page: Page, gw: LiveGateway, fake_api) -> None:  # noqa: ANN001
    add_members(gw, api_body("api-c", fake_api))
    put_order(gw, "rt", [[("api-a", "low", "high")], [("api-c", "high", "low")], ["q-local"]])
    set_enabled(gw, "api-c", False)
    until = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=3)
    iso = set_cooldown(gw, "api-a", until)
    add_members(gw, api_body("api-new", fake_api))          # 目標新增成員 → 未排序
    open_editor(page, gw, "rt")
    initial = effective_slots(gw, "rt")
    assert editor_slots(page) == initial
    assert "api-c" in sum(initial, []) and "api-new" in sum(initial, [])
    expect(card(page, "api-c").locator('[data-mark="disabled"]')).to_have_text("停用")
    import_module(page, "ui.js")
    local = page.evaluate("(iso) => window.__mods['ui.js'].fmtLocal(iso)", iso)
    expect(card(page, "api-a").locator('[data-mark="cooling"]')).to_have_text(f"冷卻至 {local}")
    for dep in ("api-b", "api-new"):
        expect(card(page, dep).locator('[data-mark="unsorted"]')).to_have_text("未排序")
    expect(card(page, "api-a").locator('[data-mark="unsorted"]')).to_have_count(0)
    # 改動其他卡片後儲存:停用、冷卻成員的 slot、effort、weight 不變;未排序成員也寫入
    card(page, "api-b").locator('select[data-role="move"]').select_option(label="Slot 1")
    page.locator("#btn-save").click()
    expect(page.locator("#dirty-banner")).to_be_hidden()
    order = route_order_full(gw, "rt")
    assert order[0] == [{"deployment": "api-a", "effort": "low", "weight": "high"},
                        {"deployment": "api-b", "effort": None, "weight": "medium"}]
    assert order[1] == [{"deployment": "api-c", "effort": "high", "weight": "low"}]
    assert sorted(e["deployment"] for s in order for e in s) == ["api-a", "api-b", "api-c", "api-new", "q-local"]
    expect(card(page, "api-new").locator('[data-mark="unsorted"]')).to_have_count(0)
    expect(card(page, "api-c").locator('[data-mark="disabled"]')).to_have_text("停用")


def test_stale_members_hidden_and_cleared_on_save(page: Page, gw: LiveGateway) -> None:
    put_order(gw, "rt", [["api-a"], ["api-b"]])
    body = api(gw, "GET", "/api/deployments/api-b").json()
    for k in ("cooling_until", "cooling_reason"):
        body.pop(k)
    body["tags"] = ["r", "only-b"]
    api(gw, "PUT", "/api/deployments/api-b", body)
    api(gw, "PUT", "/api/routes/rt", {"target": {"tags": {"any": ["only-b"]}}, "mode": "task"})
    assert route_targets(gw, "rt") == ["api-a", "api-b"]          # api-a 為 stale
    open_editor(page, gw, "rt")
    assert editor_slots(page) == [["api-b"]]
    card(page, "api-b").locator('select[data-role="weight"]').select_option("high")
    page.locator("#btn-save").click()
    expect(page.locator("#dirty-banner")).to_be_hidden()
    assert route_targets(gw, "rt") == ["api-b"]
    assert route_order_full(gw, "rt") == [[{"deployment": "api-b", "effort": None, "weight": "high"}]]


def test_empty_slots_go_through_restore_confirm(page: Page, gw: LiveGateway) -> None:
    put_order(gw, "rt", [["api-a"], ["api-b"]])
    api(gw, "PUT", "/api/routes/rt", {"target": {"tags": {"all": ["nobody"]}}, "mode": "task"})
    open_editor(page, gw, "rt")
    assert editor_slots(page) == []
    expect(page.locator("#order-source")).to_have_text("手動")
    # 取消:不刪除
    page.locator("#btn-save").click()
    dialog = page.locator("dialog")
    expect(dialog.locator(".dialog-title")).to_have_text("還原預設順序")
    dialog.get_by_role("button", name="取消").click()
    expect(dialog).to_have_count(0)
    assert route_targets(gw, "rt") == ["api-a", "api-b"]
    # 確認:等同還原預設
    page.locator("#btn-save").click()
    page.locator("dialog").get_by_role("button", name="還原預設").click()
    expect(page.locator("#order-source")).to_have_text("預設")
    assert route_targets(gw, "rt") == []


def test_restore_default(page: Page, gw: LiveGateway) -> None:
    put_order(gw, "rt", [["q-local"], ["api-b"], ["api-a"]])
    open_editor(page, gw, "rt")
    assert editor_slots(page) == [["q-local"], ["api-b"], ["api-a"]]
    page.locator("#btn-restore").click()
    dialog = page.locator("dialog")
    expect(dialog.locator(".dialog-title")).to_have_text("還原預設順序")
    expect(dialog.get_by_role("button", name="取消")).to_be_focused()
    dialog.get_by_role("button", name="取消").click()
    assert route_order(gw, "rt") == [["q-local"], ["api-b"], ["api-a"]]
    page.locator("#btn-restore").click()
    page.locator("dialog").get_by_role("button", name="還原預設").click()
    expect(page.locator("#order-source")).to_have_text("預設")
    assert route_order(gw, "rt") == []
    assert editor_slots(page) == [["api-a", "api-b"], ["q-local"]]


def test_settings_save_without_freezing_order(page: Page, gw: LiveGateway) -> None:
    open_editor(page, gw, "rt")
    page.locator("#route-record").select_option(label="關")
    expect(page.locator("#dirty-banner")).to_be_visible()
    page.locator("#route-tags").fill("daily, ops")
    page.locator("#route-precheck").check()
    page.locator("#route-mode").select_option("chat")
    expect(page.locator("#chat-note")).to_be_visible()
    page.locator("#btn-save").click()
    expect(page.locator("#dirty-banner")).to_be_hidden()
    r = api(gw, "GET", "/api/routes/rt").json()
    assert (r["record_context"], r["tags"], r["budget_precheck"], r["mode"]) == (False, ["daily", "ops"], True, "chat")
    assert r["order"] == []                                     # 只改設定:順序仍為預設
    expect(page.locator("#order-source")).to_have_text("預設")
    page.locator("#route-record").select_option(label="依全域預設")
    page.locator("#btn-save").click()
    expect(page.locator("#dirty-banner")).to_be_hidden()
    assert api(gw, "GET", "/api/routes/rt").json()["record_context"] is None
    page.locator("#route-record").select_option(label="開")
    page.locator("#btn-save").click()
    expect(page.locator("#dirty-banner")).to_be_hidden()
    assert api(gw, "GET", "/api/routes/rt").json()["record_context"] is True


def test_dry_run_panel(page: Page, gw: LiveGateway, fake_api) -> None:  # noqa: ANN001
    add_members(gw, api_body("api-off", fake_api, enabled=False))
    create_route(gw, "cloud", {"tags": {"all": ["r"], "none": ["local-only"]}}, mode="chat")
    put_order(gw, "cloud", [["api-b", "api-a"], ["api-off"]])
    api(gw, "PUT", "/api/deployments/q-local", {**local_body("q-local", fake_api), "tags": ["r", "local-only"]})
    open_editor(page, gw, "cloud")
    page.locator("#btn-dry-run").click()
    result = page.locator("#dry-result")
    expect(result.locator("#dry-first")).to_have_text(re.compile(r"^api-(a|b)$"))
    expect(result.locator("#dry-slots > li")).to_have_count(1)
    assert sorted(result.locator("#dry-slots li .mono[data-deployment]").all_inner_texts()) == ["api-a", "api-b"]
    expect(result.locator('#dry-excluded li[data-deployment="api-off"]')).to_contain_text("停用")
    expect(result.locator('#dry-excluded li[data-deployment="api-off"] [title="disabled"]')).to_have_count(1)
    expect(page.locator("#dry-random")).to_have_text("slot 內依權重隨機抽選,每次順序可能不同")


@pytest.mark.expect_http_errors(422)
def test_member_not_in_target_shows_422(page: Page, gw: LiveGateway) -> None:
    open_editor(page, gw, "rt")
    # 另一分頁先改了 route target:api-a 不再屬於目標
    body = api(gw, "GET", "/api/deployments/api-b").json()
    for k in ("cooling_until", "cooling_reason"):
        body.pop(k)
    body["tags"] = ["r", "only-b"]
    api(gw, "PUT", "/api/deployments/api-b", body)
    api(gw, "PUT", "/api/routes/rt", {"target": {"tags": {"any": ["only-b"]}}, "mode": "task"})
    card(page, "api-a").locator('select[data-role="move"]').select_option("new")
    with page.expect_response(lambda r: r.url.endswith("/api/routes/rt/order")) as resp:
        page.locator("#btn-save").click()
    assert resp.value.status == 422
    expect(page.locator("#msgbar")).to_contain_text("成員不屬於 route rt 的目標")
    expect(page.locator("#msgbar")).to_contain_text("api-a")
    expect(page.locator("#dirty-banner")).to_be_visible()
    assert route_order(gw, "rt") == []


@pytest.mark.expect_http_errors(404)
def test_editor_missing_or_unknown_route(page: Page, gw: LiveGateway) -> None:
    goto(page, gw, "/ui/route.html")
    expect(page.locator("#msgbar")).to_contain_text("缺少 route 名稱")
    open_url = "/ui/route.html?name=" + "nope%3Cimg%20src%3Dx%3E"
    goto(page, gw, open_url)
    expect(page.locator("#editor")).to_have_attribute("data-state", "error")
    expect(page.locator("#msgbar")).to_contain_text("nope<img src=x>")
    expect(page.locator("main img")).to_have_count(0)


# ---- L2:清單頁 ---------------------------------------------------------------------------------

def test_routes_list_create_delete_and_record_tristate(page: Page, gw: LiveGateway) -> None:
    api(gw, "POST", "/api/groups", {"name": "coding", "selector": {"any": ["r"]}}, expect=(201,))
    create_route(gw, "tagged", {"group": "coding"}, mode="chat", tags=["daily", "<img src=x onerror=alert(1)>"],
                 record_context=True, budget_precheck=True)
    goto(page, gw, "/ui/routes.html")
    row = page.locator('#routes-table tr[data-route="tagged"]')
    expect(row.locator('[data-col="mode"]')).to_have_text("chat")
    expect(row.locator('[data-col="target"]')).to_have_text("群組 coding")
    expect(row.locator('[data-col="tags"] .chip')).to_have_text(["daily", "<img src=x onerror=alert(1)>"])
    expect(row.locator('[data-col="precheck"]')).to_have_text("是")
    expect(row.locator('[data-col="record"]')).to_have_text("開")
    expect(page.locator("main img")).to_have_count(0)
    rt = page.locator('#routes-table tr[data-route="rt"]')
    expect(rt.locator('[data-col="target"]')).to_have_text("tags all: r")
    expect(rt.locator('[data-col="record"]')).to_have_text("依全域預設")
    expect(rt.get_by_role("link", name="rt")).to_have_attribute("href", "/ui/route.html?name=rt")

    form = page.locator("#route-new")
    expect(form).to_have_attribute("data-state", "ready")
    # 群組目標、記錄 context = 關
    form.locator("#new-name").fill("by-group")
    form.locator("#new-mode").select_option("task")
    form.locator("#new-target-kind").select_option("group")
    form.locator("#new-group").select_option("coding")
    form.locator("#new-tags").fill("weekly")
    form.locator("#new-record").select_option(label="關")
    form.locator("#new-submit").click()
    expect(page.locator('#routes-table tr[data-route="by-group"]')).to_have_count(1)
    r = api(gw, "GET", "/api/routes/by-group").json()
    assert (r["mode"], r["target"], r["tags"], r["record_context"], r["budget_precheck"]) == \
        ("task", {"group": "coding"}, ["weekly"], False, False)
    # tags 目標、預設記錄 context(null)、預估檢查
    form.locator("#new-name").fill("by-tags")
    form.locator("#new-target-kind").select_option("tags")
    form.locator("#new-any").fill("r, other")
    form.locator("#new-none").fill("slow")
    form.locator("#new-tags").fill("")
    form.locator("#new-record").select_option(label="依全域預設")
    form.locator("#new-precheck").check()
    form.locator("#new-submit").click()
    expect(page.locator('#routes-table tr[data-route="by-tags"]')).to_have_count(1)
    r = api(gw, "GET", "/api/routes/by-tags").json()
    assert (r["target"], r["record_context"], r["budget_precheck"]) == \
        ({"tags": {"all": [], "any": ["r", "other"], "none": ["slow"]}}, None, True)
    # 記錄 context = 開
    form.locator("#new-name").fill("rec-on")
    form.locator("#new-record").select_option(label="開")
    form.locator("#new-submit").click()
    expect(page.locator('#routes-table tr[data-route="rec-on"] [data-col="record"]')).to_have_text("開")
    assert api(gw, "GET", "/api/routes/rec-on").json()["record_context"] is True

    # 刪除:先取消,再確認
    page.get_by_role("button", name="刪除 by-group").click()
    page.locator("dialog").get_by_role("button", name="取消").click()
    expect(page.locator('#routes-table tr[data-route="by-group"]')).to_have_count(1)
    page.get_by_role("button", name="刪除 by-group").click()
    expect(page.locator("dialog .dialog-title")).to_have_text("刪除 route by-group")
    page.locator("dialog").get_by_role("button", name="刪除").click()
    expect(page.locator('#routes-table tr[data-route="by-group"]')).to_have_count(0)
    api(gw, "GET", "/api/routes/by-group", expect=(404,))

    # 名稱連到編輯頁
    page.get_by_role("link", name="rt", exact=True).click()
    expect(page.locator("#editor")).to_have_attribute("data-state", "ready")
    expect(page.locator("#route-name")).to_have_text("rt")


@pytest.mark.expect_http_errors(409)
def test_routes_list_create_conflict_shows_message(page: Page, gw: LiveGateway) -> None:
    goto(page, gw, "/ui/routes.html")
    form = page.locator("#route-new")
    expect(form).to_have_attribute("data-state", "ready")
    expect(form.locator("#new-target-kind")).to_have_value("tags")      # 沒有群組時預設 tags
    form.locator("#new-name").fill("rt")
    form.locator("#new-submit").click()
    expect(page.locator("#msgbar")).to_contain_text("route 已存在")

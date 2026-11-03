"""m4b_ui.feature 專用步驟(CHG-20261103-01 T9;決策 22、23,決策審議修訂 AM-5、AM-9)。

驅動方式:
- 閘道:行程內 live gateway(`tests/helpers/live_gateway.start_live_gateway`;背景 thread 跑真的 uvicorn),
  資料目錄為 environment.py 準備的暫存 `SPILLWAY_HOME`;存於 `context.ui_gw`(另一個空資料目錄的閘道為
  `context.ui_gw2`)。**不使用 `context.gw`**(它以 `asyncio.Runner` 在主 thread 執行,與 Playwright sync API
  衝突;Global Constraints)。查伺服端狀態只用同步的 `gw.http()`(經 `tests.helpers.ui.api`)與
  `gw.call_in_loop`。
- 瀏覽器:environment.py 的 `before_feature` 只為本 feature 啟動 Playwright(`context.browser`);每個情境以
  `tests.helpers.ui.open_context` 開一個 context(1280×800、Asia/Taipei、zh-TW、剪貼簿權限),收尾時
  `close_and_collect`:console error、pageerror、非本機請求任何一筆即讓情境失敗。
- 頁面操作與種資料一律匯入 `tests/helpers/ui.py` 與 `tests/helpers/ui_<area>.py`(不搬移、不複製)。
- 上游一律為 environment.py 的假 API 上游(`context.fake`);api 成員的憑證參照為 `env:SPILLWAY_TEST_KEY`
  (值為 environment.py 設定的假金鑰,只是參照)。
- 收尾登記到 `context.scenario_teardown`(environment.py 在關閉 `context.gw` 與刪除暫存目錄之前執行):
  先關瀏覽器 context 並檢查違規,再停本情境啟動的全部閘道。
- 情境 2 的「不超過 10 分鐘」只是回歸護欄;AC-8 的計時以擁有者步驟為準(決策 23)。
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import yaml
from behave import step, use_step_matcher
from playwright.sync_api import Page, Response, expect

from spillway.api.mcp import mcp_catalog
from tests.features.support.gateway import TEST_KEY, TEST_KEY_ENV
from tests.helpers.live_gateway import start_live_gateway
from tests.helpers.ui import (CLIP, NAV, add_member, add_route, api, close_and_collect, confirm_dialog,
                              db_snapshot, goto, guard_of, open_context, seed_group, wait_loaded)
from tests.helpers.ui_ledger import insert_ledger
from tests.helpers.ui_mcp import open_reference
from tests.helpers.ui_members import BROKEN_PROVIDER, MemberForm, broken_adapter_dir, open_members, seed_member
from tests.helpers.ui_overview import set_cooldown
from tests.helpers.ui_routes import (add_members, api_body, create_route, drag_slot, editor_slots, open_editor,
                                     put_order, route_order)
from tests.helpers.ui_settings import (choose_import, export_yaml, import_dialog, import_requests, open_settings,
                                       page_leaks, token_hash)

use_step_matcher("re")

REPO = Path(__file__).resolve().parents[3]
CLAUDE_MODELS = REPO / "src" / "spillway" / "adapters" / "claude_cli" / "models.yaml"
OTHER_TEXT = "其他(手動輸入)"
TPE = ZoneInfo("Asia/Taipei")
WIZARD_LIMIT_S = 600.0                # 情境 2:回歸護欄(AC-8 的計時以擁有者步驟為準)
# 情境 4:`tests.contract.fakemods.write_module` 的預設 kinds 為 ("api",);無法 import 的模組在
# /api/adapters 回 kinds: [],畫面列在所有類型,所以「宣告的類型」取其預設值。
BROKEN_KIND = "api"
SCREENS = {"總覽": "/ui/", "成員": "/ui/members.html", "帳本": "/ui/ledger.html", "設定": "/ui/settings.html",
           "API 參考": "/ui/api-reference.html"}


# ---------------------------------------------------------------- 閘道、頁面與收尾

def _gw(context: Any) -> Any:
    return context.ui_gw


def _page(context: Any) -> Page:
    return context.ui_page


def _start_gateway(context: Any, db_path: Path | None = None, **kw: Any) -> Any:
    gw = start_live_gateway(db_path, **kw)
    context.ui_gateways.append(gw)
    return gw


def _teardown(context: Any, opened: list[Any]) -> None:
    """先關瀏覽器 context 並收集違規,再停全部閘道(頁面不會看到閘道停機造成的連線錯誤)。"""
    errors: list[BaseException] = []
    problems = [str(v) for v in close_and_collect(opened)]
    for gw in reversed(context.ui_gateways):
        try:
            gw.stop()
        except BaseException as exc:  # noqa: BLE001 — 全部停完再回報
            errors.append(exc)
    context.ui_gateways = []
    if problems:
        raise AssertionError("畫面違規(console error / pageerror / 非本機請求):\n" + "\n".join(problems))
    if errors:
        raise errors[0]


@step(r"一個以假 API 上游啟動的閘道與瀏覽器")
def step_gateway_and_browser(context: Any) -> None:
    assert getattr(context, "browser", None) is not None, "m4b_ui 的 Playwright 未由 before_feature 啟動"
    context.ui_gateways = []
    ctx, guard = open_context(context.browser, permissions=CLIP)
    opened = [(ctx, guard)]
    context.scenario_teardown.append(lambda: _teardown(context, opened))
    context.ui_gw = _start_gateway(context)
    context.ui_page = ctx.new_page()


def _open_screen(context: Any, name: str) -> None:
    page, gw = _page(context), _gw(context)
    if name == "API 參考":
        open_reference(page, gw)
    elif name == "設定":
        open_settings(page, gw)
    else:
        goto(page, gw, SCREENS[name])


@step(r'開啟畫面 "([^"]+)"')
def step_open_screen(context: Any, name: str) -> None:
    context.ui_opened_at = time.monotonic()
    _open_screen(context, name)


def _local(dt: datetime) -> str:
    return dt.astimezone(TPE).strftime("%Y/%m/%d %H:%M:%S")


# ---------------------------------------------------------------- 情境 1:總覽(AC-6)

def _row(page: Page, dep_id: str) -> Any:
    return page.locator(f'#members-body tr[data-member="{dep_id}"]')


def _monthly_10(context: Any, dep_id: str) -> None:
    gw = _gw(context)
    add_member(gw, dep_id, budget={"type": "usd", "limit_amount": 10, "window": "monthly", "reset_anchor": 1})
    if not getattr(context, "ui_route", False):            # 有 route 才不會被導向精靈
        add_route(gw, "rt")
        context.ui_route = True


@step(r'成員 "([^"]+)" 額度為每月 10 美元且已花費 4 美元')
def step_member_spent(context: Any, dep_id: str) -> None:
    _monthly_10(context, dep_id)
    now = datetime.now(timezone.utc)
    insert_ledger(_gw(context), 1, now - timedelta(seconds=1), deployment=dep_id, cost_usd=4.0)


@step(r'成員 "([^"]+)" 額度為每月 10 美元且冷卻至 1 小時後')
def step_member_cooling(context: Any, dep_id: str) -> None:
    _monthly_10(context, dep_id)
    until = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0)
    set_cooldown(_gw(context), dep_id, until, "rate_limited")
    context.ui_cool_until = until


@step(r'成員 "([^"]+)" 的列顯示剩餘額度 "([^"]+)"')
def step_row_remaining(context: Any, dep_id: str, text: str) -> None:
    page = _page(context)
    wait_loaded(page, "#members-table")
    expect(_row(page, dep_id).locator('[data-field="remaining"]')).to_have_text(text)
    dash = {d["id"]: d for d in api(_gw(context), "GET", "/api/dashboard").json()["deployments"]}
    assert f"${dash[dep_id]['remaining']:.2f}" == text


@step(r'成員 "([^"]+)" 的列顯示冷卻至的時間')
def step_row_cooling(context: Any, dep_id: str) -> None:
    cool = _row(_page(context), dep_id).locator('[data-field="cooling"]')
    expect(cool).to_have_text(f"冷卻至 {_local(context.ui_cool_until)}(限流)")
    expect(_row(_page(context), dep_id).get_by_role("button", name="清除冷卻")).to_have_count(1)


@step(r'在成員 "([^"]+)" 的列按下 "清除冷卻"')
def step_clear_cooldown(context: Any, dep_id: str) -> None:
    page = _page(context)
    with page.expect_response(lambda r: r.url.endswith(f"/api/deployments/{dep_id}/clear-cooldown")) as rr:
        _row(page, dep_id).get_by_role("button", name="清除冷卻").click()
    assert rr.value.status == 200


@step(r'成員 "([^"]+)" 的列不再顯示冷卻')
def step_row_not_cooling(context: Any, dep_id: str) -> None:
    page = _page(context)
    expect(_row(page, dep_id).locator('[data-field="cooling"]')).to_have_count(0)
    expect(_row(page, dep_id).get_by_role("button", name="清除冷卻")).to_have_count(0)
    dash = {d["id"]: d for d in api(_gw(context), "GET", "/api/dashboard").json()["deployments"]}
    assert dash[dep_id]["cooling"] is False


# ---------------------------------------------------------------- 情境 2:首次設定精靈(AC-8)

def _wait_step(page: Page, n: int) -> None:
    expect(page.locator(f'main[data-step="{n}"]')).to_have_count(1)
    expect(page.locator(f"#step-{n}")).to_be_visible()


@step(r'閘道行程的環境變數 "([^"]+)" 已設定為假金鑰')
def step_env_fake_key(context: Any, var: str) -> None:
    assert var == TEST_KEY_ENV and os.environ.get(var) == TEST_KEY       # 行程內閘道:同一個環境


@step(r"畫面導向首次設定精靈")
def step_redirected_to_wizard(context: Any) -> None:
    page = _page(context)
    expect(page).to_have_url(_gw(context).url("/ui/wizard.html"))
    page.wait_for_selector('html[data-shell="ready"]', state="attached")
    _wait_step(page, 1)


@step(r'在精靈中依序選類型 "([^"]+)"、供應商 "([^"]+)"、第一個模型,上游網址填假上游,'
      r'憑證參照填 "([^"]+)"')
def step_wizard_member(context: Any, kind: str, provider: str, cred: str) -> None:
    page = _page(context)
    form = MemberForm(page, page.locator("#member-form-host"))
    form.wait_ready()
    form.kind(kind)
    form.provider.select_option(provider)
    form.model.select_option(index=0)
    form.field("id").fill("openai-1")
    form.field("upstream").fill(f"{context.fake.base_url}/v1")
    form.field("credential_ref").fill(cred)
    form.submit.click()                                     # 「下一步 →」:① 完成即寫入
    _wait_step(page, 2)
    context.ui_member = api(_gw(context), "GET", "/api/deployments/openai-1").json()
    assert context.ui_member["credential_ref"] == cred


@step(r'在精靈中按下 "測試呼叫"')
def step_wizard_test(context: Any) -> None:
    page = _page(context)
    with page.expect_response(lambda r: r.url.endswith("/api/deployments/openai-1/test")) as rr:
        page.locator("#test-run").click()
    context.ui_test_result = rr.value.json()


@step(r"精靈顯示測試結果為成功,且假上游收到該模型的請求")
def step_wizard_test_ok(context: Any) -> None:
    page = _page(context)
    assert context.ui_test_result["ok"] is True
    expect(page.locator('#test-result[data-ok="true"] [data-role="test-summary"]')).to_contain_text("測試成功")
    assert context.ui_member["model"] in [r.model for r in context.fake.requests]


@step(r'在精靈中建立 route "([^"]+)" 並完成精靈')
def step_wizard_route(context: Any, name: str) -> None:
    page = _page(context)
    page.locator("#test-next").click()
    _wait_step(page, 3)
    page.locator("#route-name").fill(name)
    expect(page.locator("#route-preview")).to_have_text("符合 1 個成員")
    page.locator("#route-create").click()
    _wait_step(page, 4)


@step(r"精靈顯示可複製的呼叫範例")
def step_wizard_example(context: Any) -> None:
    page = _page(context)
    curl = page.locator("#curl-example")
    expect(curl).to_contain_text(f"curl http://127.0.0.1:{_gw(context).port}/v1/chat/completions")
    page.locator("#curl-copy").click()
    text = curl.text_content() or ""
    assert page.evaluate("navigator.clipboard.readText()") == text
    m = re.search(r'"model":\s*"([^"]+)"', text)
    assert m, text
    context.ui_example_model = m.group(1)


@step(r'以該範例的 model 呼叫 "(/v1/chat/completions)" 回應狀態為 200')
def step_call_example(context: Any, path: str) -> None:
    with _gw(context).http() as c:
        r = c.post(path, json={"model": context.ui_example_model,
                               "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    context.ui_called_at = time.monotonic()


@step(r"從開啟精靈到呼叫成功的時間不超過 10 分鐘")
def step_wizard_time(context: Any) -> None:
    elapsed = context.ui_called_at - context.ui_opened_at
    assert elapsed <= WIZARD_LIMIT_S, f"{elapsed:.1f} s"


# ---------------------------------------------------------------- 情境 3、4:成員表單(AC-20、AC-21)

def _adapters(context: Any) -> list[dict[str, Any]]:
    return api(_gw(context), "GET", "/api/adapters").json()["modules"]


@step(r'開啟畫面 "成員" 並按下 "新增成員"')
def step_open_new_member(context: Any) -> None:
    page = _page(context)
    open_members(page, _gw(context))
    page.locator("#new-member").click()
    context.ui_form = MemberForm(page)
    context.ui_form.wait_ready()


@step(r'選類型 "(cli|api|local)"')
def step_pick_kind(context: Any, kind: str) -> None:
    context.ui_form.kind(kind)
    context.ui_kind = kind


@step(r'供應商選單只列出支援 "([^"]+)" 的模組')
def step_providers_for_kind(context: Any, kind: str) -> None:
    form = context.ui_form
    want = [p for m in _adapters(context) if m["available"] and kind in m["kinds"] for p in m["providers"]]
    assert want and form.option_values(form.provider, enabled_only=True) == want


@step(r'選供應商 "([^"]+)" 與第一個模型')
def step_pick_provider_model(context: Any, provider: str) -> None:
    form = context.ui_form
    form.provider.select_option(provider)
    form.model.select_option(index=0)


@step(r'模型選單的項目與 "claude_cli" 模組的 models\.yaml 一致,最後一項為 "([^"]+)"')
def step_models_match_yaml(context: Any, last: str) -> None:
    form = context.ui_form
    entries = yaml.safe_load(CLAUDE_MODELS.read_text(encoding="utf-8"))
    assert last == OTHER_TEXT
    assert form.option_texts(form.model) == [e["label"] for e in entries] + [OTHER_TEXT]
    assert form.option_values(form.model)[:-1] == [e["id"] for e in entries]
    context.ui_first_model = entries[0]


@step(r"能力欄位帶入該模型的 capabilities")
def step_caps_filled(context: Any) -> None:
    form = context.ui_form
    caps = context.ui_first_model["capabilities"]
    assert form.caps() == {"ctx": caps["ctx"], "tools": caps["tools"], "vision": caps["vision"],
                           "efforts": caps["efforts"]}
    expect(form.field("model_name")).to_have_value(context.ui_first_model["id"])


@step(r"閘道以含一個 __init__\.py 無法 import 的 adapter 模組的 extra_adapter_paths 啟動")
def step_gateway_with_broken(context: Any) -> None:
    root = broken_adapter_dir(context.tmp / "mods")
    db_path = context.tmp / "spillway-home-ac21" / "spillway.db"
    gw = _start_gateway(context, db_path, extra_adapter_paths=[root])      # 正常啟動
    context.ui_gw = gw
    broken = next(m for m in _adapters(context) if m["module"] == "broken")
    assert broken["available"] is False and broken["unavailable_reason"]
    context.ui_broken = broken


@step(r"選該模組宣告的類型")
def step_pick_broken_kind(context: Any) -> None:
    context.ui_form.kind(BROKEN_KIND)


@step(r"該模組的供應商選項為停用並顯示原因")
def step_broken_disabled(context: Any) -> None:
    opt = context.ui_form.provider.locator(f'option[value="{BROKEN_PROVIDER}"]')
    expect(opt).to_have_count(1)
    expect(opt).to_be_disabled()
    expect(opt).to_contain_text(context.ui_broken["unavailable_reason"])


# ---------------------------------------------------------------- 情境 5:API 參考(AC-22)

def _catalog(context: Any, group: str = "all") -> dict[str, Any]:
    gw = _gw(context)
    return json.loads(json.dumps(gw.call_in_loop(mcp_catalog, gw.app, group)))


def _detail(page: Page) -> Any:
    return page.locator("#ref-detail")


def _show_item(page: Page, item: str) -> None:
    page.locator(f'#ref-index a[data-item="{item}"]').click()
    expect(_detail(page)).to_have_attribute("data-item", item)


@step(r'索引有 "連線"、"工具"、"通知"、"錯誤碼" 四大類')
def step_ref_categories(context: Any) -> None:
    expect(_page(context).locator("#ref-index details > summary")).to_have_text(["連線", "工具", "通知", "錯誤碼"])


@step(r'收合 "工具" 之後其項目不再顯示,再展開後恢復')
def step_ref_collapse(context: Any) -> None:
    page = _page(context)
    summary = page.locator('#ref-index details[data-category="tools"] > summary')
    tools = page.locator('#ref-index details[data-category="tools"] a')
    expect(tools.first).to_be_visible()
    summary.click()
    for link in tools.all():
        expect(link).to_be_hidden()
    summary.click()
    for link in tools.all():
        expect(link).to_be_visible()


@step(r"畫面上的工具名稱、通知與錯誤碼與 mcp_catalog 的內容一致")
def step_ref_matches_catalog(context: Any) -> None:
    page, cat = _page(context), _catalog(context)
    names = page.locator('#ref-index details[data-category="tools"] a').all_text_contents()
    assert sorted(names) == sorted(t["name"] for t in cat["tools"])
    expect(page.locator('#ref-index details[data-category="notifications"] a')).to_have_text(
        [n["method"] for n in cat["notifications"]])
    first_col = "tbody tr td:first-child"
    _show_item(page, "errors-http")
    assert _detail(page).locator(first_col).all_text_contents() == [e["code"] for e in cat["errors"]["http"]]
    _show_item(page, "errors-tool")
    assert _detail(page).locator(first_col).all_text_contents() == [e["code"] for e in cat["errors"]["tool"]]
    _show_item(page, "errors-sdk")
    assert _detail(page).locator("tbody tr td:nth-child(2)").all_text_contents() == \
        [e["message"] for e in cat["errors"]["sdk_transport"]]
    ask = next(t for t in cat["tools"] if t["name"] == "ask")
    _show_item(page, "tool-ask")
    expect(_detail(page).locator("[data-role=description]")).to_have_text(ask["description"])
    assert json.loads(_detail(page).locator("[data-role=input-schema]").text_content() or "") == ask["inputSchema"]


@step(r'畫面不含 "/api/" 與 "/v1" 字樣')
def step_ref_no_paths(context: Any) -> None:
    page = _page(context)
    items = [a.get_attribute("data-item") for a in page.locator("#ref-index a[data-item]").all()]
    assert items
    for item in items:
        _show_item(page, str(item))
        text = page.locator("body").inner_text()
        assert "/api/" not in text and "/v1" not in text, item


# ---------------------------------------------------------------- 情境 6:Route 拖曳(spec 10.1;AC-2)

@step(r'task route "([^"]+)" 的手動順序為 slot 1 "([^"]+)"、slot 2 "([^"]+)"')
def step_route_manual(context: Any, name: str, first: str, second: str) -> None:
    gw = _gw(context)
    add_members(gw, api_body(first, context.fake), api_body(second, context.fake))
    create_route(gw, name, {"tags": {"any": ["r"]}}, mode="task")
    put_order(gw, name, [[first], [second]])


@step(r'開啟 route "([^"]+)" 的編輯頁')
def step_open_editor(context: Any, name: str) -> None:
    open_editor(_page(context), _gw(context), name)


@step(r'編輯頁的 slot 依序為 "([^"]+)"、"([^"]+)"')
def step_editor_slots(context: Any, first: str, second: str) -> None:
    assert editor_slots(_page(context)) == [[first], [second]]


@step(r"把 slot 2 整列拖到 slot 1 之前")
def step_drag_slot(context: Any) -> None:
    page = _page(context)
    drag_slot(page, 1, 0)              # 索引從 0 起:slot 2 → slot 1 之前
    context.ui_slots = editor_slots(page)
    assert len(context.ui_slots) == 2 and context.ui_slots[0] != context.ui_slots[1]


@step(r"畫面頂端標示有未儲存的變更")
def step_dirty(context: Any) -> None:
    expect(_page(context).locator("#dirty-banner")).to_be_visible()
    expect(_page(context).locator("#dirty-banner")).to_contain_text("有未儲存的變更")


@step(r'按下 "儲存"')
def step_save_route(context: Any) -> None:
    page = _page(context)
    with page.expect_response(lambda r: r.url.endswith("/effective-order")) as reread:
        page.locator("#btn-save").click()
    assert reread.value.status == 200
    expect(page.locator("#dirty-banner")).to_be_hidden()


@step(r'route "([^"]+)" 的手動順序 slot 1 為 "([^"]+)"')
def step_route_first(context: Any, name: str, dep_id: str) -> None:
    order = route_order(_gw(context), name)
    assert order[0] == [dep_id], order
    assert editor_slots(_page(context)) == order


@step(r'送出 3 次 task 請求皆由 "([^"]+)" 處理')
def step_three_tasks(context: Any, dep_id: str) -> None:
    served = []
    with _gw(context).http() as c:
        for i in range(3):
            r = c.post("/v1/chat/completions", json={
                "model": "review", "metadata": {"mode": "task"},
                "messages": [{"role": "user", "content": f"m4b task {i}"}]})
            assert r.status_code == 200, r.text
            served.append(r.headers.get("X-Spillway-Served-By"))
    assert served == [dep_id] * 3, served


# ---------------------------------------------------------------- 情境 7:匯入確認對話框

def _changed_config(gw: Any, dep_id: str, new_base: str) -> bytes:
    doc = yaml.safe_load(export_yaml(gw))
    for dep in doc["deployments"]:
        if dep["id"] == dep_id:
            dep["params"]["api_base"] = new_base
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False).encode("utf-8")


def _api_base(context: Any, dep_id: str) -> str:
    return api(_gw(context), "GET", f"/api/deployments/{dep_id}").json()["params"]["api_base"]


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


@step(r'開啟畫面 "設定",選擇一份把成員 "([^"]+)" 的 params\.api_base 改掉的設定檔與 mode "(merge|replace)",'
      r'按下 "預覽匯入"')
def step_import_preview(context: Any, dep_id: str, mode: str) -> None:
    gw, page = _gw(context), _page(context)
    seed_member(gw, "api-a")
    seed_member(gw, dep_id, params={"api_base": "http://127.0.0.1:9/v1"})
    context.ui_old_base = _api_base(context, dep_id)
    context.ui_new_base = "http://127.0.0.1:10/v1"
    context.ui_import_data = _changed_config(gw, dep_id, context.ui_new_base)
    with gw.http() as c:                           # 期望值(dry_run 不寫入)
        r = c.post(f"/api/config/import?mode={mode}&dry_run=true", content=context.ui_import_data,
                   headers={"Content-Type": "application/yaml"})
    assert r.status_code == 200, r.text
    context.ui_expected = r.json()
    context.ui_snapshot = db_snapshot(gw)
    open_settings(page, gw)
    choose_import(page, context.ui_import_data, mode=mode)
    assert import_requests(guard_of(page)) == []          # 選檔與 mode 不送請求
    page.locator("#import-preview").click()
    expect(import_dialog(page)).to_be_visible()


@step(r'確認對話框列出計數與 privileged 成員 "([^"]+)",資料尚未改變')
def step_import_dialog(context: Any, dep_id: str) -> None:
    dialog, expected = import_dialog(_page(context)), context.ui_expected
    for key in ("created", "updated", "deleted"):
        for kind in ("deployments", "groups", "routes"):
            expect(dialog.locator(f'[data-count="{kind}.{key}"]')).to_have_text(str(expected[key][kind]))
    assert expected["privileged"] == [dep_id]
    expect(dialog.locator('[data-role="privileged"]')).to_have_text(dep_id)
    expect(dialog.locator('[data-role="cancel"]')).to_be_focused()
    assert db_snapshot(_gw(context)) == context.ui_snapshot


@step(r'在確認對話框按下 "取消"')
def step_import_cancel(context: Any) -> None:
    dialog = import_dialog(_page(context))
    dialog.locator('[data-role="cancel"]').click()
    expect(dialog).to_have_count(0)


@step(r'成員 "([^"]+)" 的 params\.api_base 不變,且沒有送出非 dry_run 的匯入請求')
def step_import_unchanged(context: Any, dep_id: str) -> None:
    assert _api_base(context, dep_id) == context.ui_old_base
    assert db_snapshot(_gw(context)) == context.ui_snapshot
    urls = import_requests(guard_of(_page(context)))
    assert urls and all(_query(u).get("dry_run") == ["true"] for u in urls), urls


@step(r'再次按下 "預覽匯入" 並在確認對話框按下 "匯入"')
def step_import_confirm(context: Any) -> None:
    page = _page(context)
    page.locator("#import-preview").click()
    dialog = import_dialog(page)
    expect(dialog).to_be_visible()
    with page.expect_response(lambda r: "/api/config/import" in r.url and "dry_run" not in r.url) as rr:
        dialog.locator('[data-role="confirm"]').click()
    assert rr.value.status == 200
    context.ui_import_result = rr.value.json()


@step(r'畫面顯示備份檔名,成員 "([^"]+)" 的 params\.api_base 已改變')
def step_import_done(context: Any, dep_id: str) -> None:
    result, gw = context.ui_import_result, _gw(context)
    assert result["dry_run"] is False and result["backup"]
    expect(_page(context).locator('#import-result [data-role="backup"]')).to_have_text(f"backups/{result['backup']}")
    assert (gw.db_path.parent / "backups" / result["backup"]).is_file()
    assert _api_base(context, dep_id) == context.ui_new_base


# ---------------------------------------------------------------- 情境 8:使用者資料以純文字顯示

XSS_TAG = "<img src=x onerror=alert(1)>"
XSS_ERROR = "<script>alert(1)</script>"


@step(r'成員 "([^"]+)" 的 tags 含 "(<img src=x onerror=alert\(1\)>)"')
def step_xss_tag(context: Any, dep_id: str, tag: str) -> None:
    gw = _gw(context)
    add_member(gw, dep_id, kind="local", tags=[tag, "r"])
    add_route(gw, "rt")


@step(r'帳本有一列的 error 為 "(<script>alert\(1\)</script>)"')
def step_xss_ledger(context: Any, error: str) -> None:
    insert_ledger(_gw(context), 1, datetime.now(timezone.utc) - timedelta(minutes=5), deployment="api-a",
                  status="error", error=error)


@step(r'依序開啟畫面 "總覽"、"成員"、"帳本"')
def step_open_three(context: Any) -> None:
    page, gw = _page(context), _gw(context)
    seen: dict[str, dict[str, Any]] = {}
    goto(page, gw, "/ui/")
    wait_loaded(page, "#members-table")
    expect(_row(page, "api-a").locator(".chip").first).to_have_text(XSS_TAG)
    seen["總覽"] = _page_facts(page)
    open_members(page, gw)
    expect(page.locator("#member-table-host tbody tr").first).to_contain_text(XSS_TAG)
    seen["成員"] = _page_facts(page)
    goto(page, gw, "/ui/ledger.html")
    wait_loaded(page, "#ledger-table")
    expect(page.locator('#ledger-body tr[data-ledger-id="1"] .ledger-error')).to_have_text(XSS_ERROR)
    seen["帳本"] = _page_facts(page)
    context.ui_seen = seen


def _page_facts(page: Page) -> dict[str, Any]:
    return {"text": page.locator("main").inner_text(),
            "img": page.evaluate("() => document.querySelectorAll('img').length"),
            "script": page.evaluate("() => document.querySelectorAll('script').length"),
            "xss": page.evaluate("() => window.__xss === undefined ? null : window.__xss")}


@step(r"畫面上以文字顯示這兩個字串")
def step_xss_as_text(context: Any) -> None:
    seen = context.ui_seen
    assert XSS_TAG in seen["總覽"]["text"] and XSS_TAG in seen["成員"]["text"]
    assert XSS_ERROR in seen["帳本"]["text"]


@step(r"頁面沒有執行任何對話框,也沒有新增 img 或 script 元素")
def step_xss_inert(context: Any) -> None:
    for name, facts in context.ui_seen.items():
        assert facts["img"] == 0, name
        assert facts["script"] == 1, name          # 只有頁面自己的 module script
        assert facts["xss"] is None, name
    assert guard_of(_page(context)).dialogs == []


# ---------------------------------------------------------------- 情境 9:token 與金鑰不出現在畫面(NFR-4 畫面部分)

@step(r'已產生過存取 token,且成員 "([^"]+)" 的憑證參照指向值為假金鑰的環境變數')
def step_token_and_key(context: Any, dep_id: str) -> None:
    gw = _gw(context)
    assert gw.token and token_hash(gw)
    dep = add_member(gw, dep_id, params={"api_base": f"{context.fake.base_url}/v1"})
    assert dep["credential_ref"] == f"env:{TEST_KEY_ENV}" and os.environ[TEST_KEY_ENV] == TEST_KEY
    add_route(gw, "rt")
    seed_group(gw, "coding", {"any": ["r"]})


@step(r"依序開啟全部 8 個畫面")
def step_open_all(context: Any) -> None:
    page, gw = _page(context), _gw(context)
    captured: list[tuple[str, str]] = []          # (來源, 內容)
    responses: list[Response] = []
    page.on("response", lambda r: responses.append(r) if "/api/" in r.url else None)
    for label, href in NAV:
        responses.clear()
        goto(page, gw, href)
        page.wait_for_load_state("networkidle")
        assert page.locator("main h1").first.inner_text().strip(), label
        captured.append((f"html:{label}", page.content()))
        for r in list(responses):
            captured.append((f"api:{label}:{r.url}", r.text()))
    context.ui_captured = captured


@step(r"每個畫面的 HTML 與畫面呼叫的 API 回應都不含該 token 與假金鑰值")
def step_no_secrets(context: Any) -> None:
    captured = context.ui_captured
    assert sum(1 for k, _ in captured if k.startswith("html:")) == 8
    assert any(k.startswith("api:") for k, _ in captured)
    for secret in (_gw(context).token, TEST_KEY):
        hits = [k for k, v in captured if secret in v]
        assert hits == [], hits


@step(r'在畫面 "設定" 按下 "重新產生 token" 並確認')
def step_rotate(context: Any) -> None:
    page, gw = _page(context), _gw(context)
    open_settings(page, gw)
    page.locator("#token-rotate").click()
    confirm = confirm_dialog(page)
    expect(confirm.locator(".dialog-title")).to_have_text("重新產生 token")
    with page.expect_response(lambda r: r.url.endswith("/api/settings/token/rotate")) as rr:
        confirm.locator('[data-role="confirm"]').click()
    context.ui_new_token = rr.value.json()["token"]


@step(r"對話框顯示新 token 一次;關閉對話框後頁面 DOM 與輸入欄位都不含新 token")
def step_token_once(context: Any) -> None:
    page, token = _page(context), context.ui_new_token
    dlg = page.locator("#token-dialog")
    expect(dlg.locator("#token-value")).to_have_value(token)
    expect(dlg.locator("#token-export")).to_have_text(f"export SPILLWAY_TOKEN={token}")
    assert token != _gw(context).token
    dlg.locator('[data-role="copy"]').click()
    expect(page.locator("#msgbar")).to_contain_text("已複製")
    dlg.locator('[data-role="close"]').click()
    expect(dlg).to_have_count(0)
    assert page_leaks(page, token) == []


@step(r'重新整理後畫面只顯示 "已產生"')
def step_after_reload(context: Any) -> None:
    page = _page(context)
    page.reload()
    page.wait_for_selector('#overview[data-loaded="true"]', state="attached")
    expect(page.locator("#token-status")).to_have_text("已產生")
    expect(page.locator("#ov-token")).to_have_text("已產生")
    assert page_leaks(page, context.ui_new_token) == []


# ---------------------------------------------------------------- 情境 10:經畫面匯出、replace 匯入、再匯出(AC-10)

@step(r"已有 2 個成員、1 個群組與 1 條有手動順序的 route")
def step_seed_config(context: Any) -> None:
    gw = _gw(context)
    add_members(gw, api_body("api-a", context.fake), api_body("api-b", context.fake))
    seed_group(gw, "coding", {"any": ["r"]})
    create_route(gw, "review", {"group": "coding"}, mode="task")
    put_order(gw, "review", [["api-b"], ["api-a"]])


def _export_via_screen(page: Page, gw: Any, dest: Path) -> bytes:
    open_settings(page, gw)
    with page.expect_download() as dl:
        page.locator("#export-link").click()
    data = Path(dl.value.path()).read_bytes()
    dest.write_bytes(data)
    assert data == export_yaml(gw).encode("utf-8")
    return data


@step(r'在畫面 "設定" 匯出設定為檔案 A')
def step_export_a(context: Any) -> None:
    context.ui_file_a = _export_via_screen(_page(context), _gw(context), context.tmp / "A.yaml")


@step(r'在另一個空資料目錄的閘道開啟畫面 "設定",以 mode "replace" 預覽匯入檔案 A 並在確認對話框按下 "匯入"')
def step_import_a_elsewhere(context: Any) -> None:
    gw2 = _start_gateway(context, context.tmp / "spillway-home-2" / "spillway.db")
    context.ui_gw2 = gw2
    page = _page(context)
    open_settings(page, gw2)
    choose_import(page, context.ui_file_a, mode="replace")
    page.locator("#import-preview").click()
    dialog = import_dialog(page)
    expect(dialog).to_be_visible()
    with page.expect_response(lambda r: "/api/config/import" in r.url and "dry_run" not in r.url) as rr:
        dialog.locator('[data-role="confirm"]').click()
    assert rr.value.status == 200
    result = rr.value.json()
    assert result["mode"] == "replace" and result["dry_run"] is False
    expect(page.locator("#import-result")).to_be_visible()


@step(r'在該閘道的畫面 "設定" 匯出設定為檔案 B')
def step_export_b(context: Any) -> None:
    context.ui_file_b = _export_via_screen(_page(context), context.ui_gw2, context.tmp / "B.yaml")


@step(r"檔案 A 與 B 逐位元組相同,且 A 不含假金鑰值")
def step_a_equals_b(context: Any) -> None:
    a, b = context.ui_file_a, context.ui_file_b
    assert a == b
    assert TEST_KEY.encode() not in a
    assert _gw(context).token.encode() not in a

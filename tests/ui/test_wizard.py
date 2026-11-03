"""首次設定精靈(CHG-20261103-01 T8 L2;決策 18、25,決策審議修訂 AM-8;設計差異 #10)。

空資料庫走完四步(假 API 上游)後 `/v1/chat/completions` 以 ③ 的 route 呼叫成功;② 成功後假上游收到該 model;
`mountMemberForm` 以 `{defaults: {kind: "api", tags: ["default"]}, requireTag: true}` 掛載(T5 契約);
移除所有 tag 時不可前進;tag 選單依輸入順序並以 `POST /api/groups/preview` 顯示符合數;`chat` 已存在(409)
時沿用並進入 ④;選 cli 時 mode 預設 task;測試呼叫失敗時顯示錯誤並可回上一步;只完成 ① 就離開時 UI-1 顯示
續作橫幅、再開啟從 ③ 繼續;都有時從 ④;④ 連到 `settings.html#token`;「略過」到 `/ui/?skip_wizard=1`。
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.helpers import TEST_KEY, TEST_KEY_ENV, dead_base
from tests.helpers.live_gateway import LiveGateway
from tests.helpers.ui import MakeGw, NewPage, add_route, api, goto, guard_of
from tests.helpers.ui_members import MemberForm, requests_of, seed_member
from tests.helpers.ui_settings import open_wizard, wizard_step

CLIP = ["clipboard-read", "clipboard-write"]


@pytest.fixture
def gw(make_ui_gateway: MakeGw) -> LiveGateway:
    return make_ui_gateway()


def step_card(page: Page, n: int):  # noqa: ANN201
    return page.locator(f"#step-{n}")


def wait_step(page: Page, n: int) -> None:
    expect(page.locator(f'main[data-step="{n}"]')).to_have_count(1)
    expect(step_card(page, n)).to_be_visible()
    expect(page.locator(f'#wizard-steps li[data-step="{n}"]')).to_have_attribute("aria-current", "step")


def fill_api_member(page: Page, dep_id: str, upstream: str) -> MemberForm:
    form = MemberForm(page, page.locator("#member-form-host"))
    form.wait_ready()
    form.kind("api")
    form.provider.select_option("openai")
    form.model.select_option(index=0)
    form.field("id").fill(dep_id)
    form.field("upstream").fill(upstream)
    form.field("credential_ref").fill(f"env:{TEST_KEY_ENV}")
    return form


def test_empty_db_full_flow_ac8(new_page: NewPage, gw: LiveGateway, fake_api) -> None:  # noqa: ANN001
    page = new_page(permissions=CLIP)
    page.goto(gw.url("/ui/"))
    expect(page).to_have_url(gw.url("/ui/wizard.html"))      # first_run → 精靈
    page.wait_for_selector('html[data-shell="ready"]', state="attached")
    wait_step(page, 1)
    assert page.locator('link[rel="stylesheet"][href="assets/member-form.css"]').count() == 1
    expect(step_card(page, 1)).to_contain_text("憑證參照的環境變數必須在啟動 serve 前設定;值不要貼在這裡")

    form = MemberForm(page, page.locator("#member-form-host"))
    form.wait_ready()
    expect(form.form.locator('button[data-kind="api"]')).to_have_attribute("aria-pressed", "true")
    expect(form.form.locator('[data-role="tags"] .chip')).to_have_text(["default×"])
    expect(form.submit).to_have_text("下一步 →")
    fill_api_member(page, "openai-1", f"{fake_api.base_url}/v1")
    form.submit.click()

    wait_step(page, 2)
    dep = api(gw, "GET", "/api/deployments/openai-1").json()        # ① 完成即寫入
    assert dep["tags"] == ["default"] and dep["params"] == {"api_base": f"{fake_api.base_url}/v1"}
    expect(page.locator("#test-note")).to_have_text("會實際呼叫上游一次,並記一列帳本")
    expect(page.locator("#test-member")).to_contain_text("openai-1")
    with page.expect_response(lambda r: r.url.endswith("/api/deployments/openai-1/test")) as rr:
        page.locator("#test-run").click()
    assert rr.value.json()["ok"] is True
    expect(page.locator('#test-result[data-ok="true"] [data-role="test-summary"]')).to_contain_text("測試成功")
    assert dep["model"] in [r.model for r in fake_api.requests]       # 假上游收到該 model
    assert [r["deployment_id"] for r in api(gw, "GET", "/api/ledger").json()["items"]] == ["openai-1"]
    page.locator("#test-next").click()

    wait_step(page, 3)
    expect(page.locator("#route-name")).to_have_value("chat")
    expect(page.locator("#route-mode")).to_have_value("chat")
    expect(page.locator("#route-cli-note")).to_be_hidden()
    expect(page.locator("#route-tag option")).to_have_text(["default"])
    expect(page.locator("#route-preview")).to_have_text("符合 1 個成員")
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/api/routes")) as req:
        page.locator("#route-create").click()
    assert req.value.post_data_json == {"name": "chat", "mode": "chat", "target": {"tags": {"any": ["default"]}}}

    wait_step(page, 4)
    curl = page.locator("#curl-example")
    expect(curl).to_contain_text(f"curl http://127.0.0.1:{gw.port}/v1/chat/completions")
    expect(curl).to_contain_text('"model":"chat"')
    expect(curl).to_have_attribute("data-route", "chat")
    page.locator("#curl-copy").click()
    assert page.evaluate("navigator.clipboard.readText()") == curl.text_content()
    token_link = page.locator("#done-token")
    expect(token_link).to_have_text("前往設定產生")
    expect(token_link).to_have_attribute("href", "/ui/settings.html#token")
    expect(step_card(page, 4)).to_contain_text("MCP 用戶端需要存取 token:前往設定產生")
    expect(page.locator("#done-overview")).to_have_text("前往總覽")
    # 以範例的 model 呼叫 /v1
    with gw.http() as c:
        r = c.post("/v1/chat/completions", json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    assert TEST_KEY not in page.content()
    page.locator("#done-overview").click()
    expect(page).to_have_url(gw.url("/ui/"))
    expect(page.locator("main h1")).to_have_text("總覽")


def test_tags_required_and_order_and_preview(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_wizard(page, gw)
    wait_step(page, 1)
    form = fill_api_member(page, "w1", "http://127.0.0.1:9/v1")
    form.form.locator('button[aria-label="移除 default"]').click()
    form.submit.click()
    expect(form.error).to_have_text("至少需要 1 個 tag")
    assert requests_of(guard_of(page), "POST", "/api/deployments") == []
    assert wizard_step(page) == 1
    form.add_tag("zeta")
    form.add_tag("alpha")
    form.submit.click()
    wait_step(page, 2)
    assert api(gw, "GET", "/api/deployments/w1").json()["tags"] == ["alpha", "zeta"]   # API 依字母排序
    seed_member(gw, "other", tags=["alpha"])
    page.locator("#test-next").click()
    wait_step(page, 3)
    expect(page.locator("#route-tag option")).to_have_text(["zeta", "alpha"])        # 畫面保存輸入順序
    expect(page.locator("#route-tag")).to_have_value("zeta")
    expect(page.locator("#route-preview")).to_have_text("符合 1 個成員")
    with page.expect_request(lambda r: r.url.endswith("/api/groups/preview")) as req:
        page.locator("#route-tag").select_option("alpha")
    assert req.value.post_data_json == {"selector": {"all": [], "any": ["alpha"], "none": []}}
    expect(page.locator("#route-preview")).to_have_text("符合 2 個成員")
    page.locator("#route-name").fill("my route")
    page.locator("#route-create").click()
    expect(page.locator("#route-error")).to_contain_text("請填寫 route 名稱")
    page.locator("#route-name").fill("tagged")
    page.locator("#route-create").click()
    wait_step(page, 4)
    rt = api(gw, "GET", "/api/routes/tagged").json()
    assert rt["target"]["tags"]["any"] == ["alpha"] and not rt["target"]["tags"].get("all")


@pytest.mark.expect_http_errors(409)
def test_existing_chat_route_is_reused(new_page: NewPage, gw: LiveGateway) -> None:
    add_route(gw, "chat", "x")
    page = new_page()
    open_wizard(page, gw)
    wait_step(page, 1)                                           # 沒有成員 → ①
    fill_api_member(page, "w2", "http://127.0.0.1:9/v1").submit.click()
    wait_step(page, 2)
    page.locator("#test-next").click()
    wait_step(page, 3)
    with page.expect_response(lambda r: r.url.endswith("/api/routes") and r.request.method == "POST") as rr:
        page.locator("#route-create").click()
    assert rr.value.status == 409
    wait_step(page, 4)
    expect(page.locator("#msgbar")).to_contain_text("route「chat」已存在,沿用既有 route")
    expect(page.locator("#curl-example")).to_have_attribute("data-route", "chat")
    assert api(gw, "GET", "/api/routes/chat").json()["target"]["tags"]["any"] == ["x"]   # 既有 route 不變


def test_cli_member_defaults_task_mode(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_wizard(page, gw)
    form = MemberForm(page, page.locator("#member-form-host"))
    form.wait_ready()
    form.kind("cli")
    form.provider.select_option("claude-cli")
    form.model.select_option(index=0)
    form.field("id").fill("claude-1")
    form.submit.click()
    wait_step(page, 2)
    assert requests_of(guard_of(page), "POST", "/test") == []    # 不執行 CLI
    page.locator("#test-next").click()
    wait_step(page, 3)
    expect(page.locator("#route-mode")).to_have_value("task")
    expect(page.locator("#route-cli-note")).to_be_visible()
    expect(page.locator("#route-cli-note")).to_have_text("chat route 不使用 CLI 成員")


def test_test_call_failure_then_back_and_fix(new_page: NewPage, gw: LiveGateway, fake_api) -> None:  # noqa: ANN001
    page = new_page()
    open_wizard(page, gw)
    fill_api_member(page, "w3", dead_base()).submit.click()
    wait_step(page, 2)
    with page.expect_response(lambda r: r.url.endswith("/api/deployments/w3/test")) as rr:
        page.locator("#test-run").click()
    assert rr.value.json()["ok"] is False
    box = page.locator('#test-result[data-ok="false"]')
    expect(box.locator('[data-role="test-summary"]')).to_contain_text("測試失敗")
    expect(box.locator('[data-role="test-hint"]')).to_contain_text("環境變數")
    expect(box.locator('[data-role="test-hint"]')).to_contain_text("上游網址")
    page.locator("#test-back").click()
    wait_step(page, 1)
    form = MemberForm(page, page.locator("#member-form-host"))
    form.wait_ready()
    expect(form.form).to_have_attribute("data-member-form", "edit")
    expect(form.field("id")).to_have_value("w3")
    form.field("upstream").fill(f"{fake_api.base_url}/v1")
    with page.expect_request(lambda r: r.method == "PUT" and r.url.endswith("/api/deployments/w3")):
        form.submit.click()
    wait_step(page, 2)
    expect(page.locator("#test-result")).to_be_hidden()
    page.locator("#test-run").click()
    expect(page.locator('#test-result[data-ok="true"]')).to_be_visible()
    assert api(gw, "GET", "/api/deployments/w3").json()["params"]["api_base"] == f"{fake_api.base_url}/v1"


def test_resume_from_step3_via_overview_banner(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_wizard(page, gw)
    form = fill_api_member(page, "w4", "http://127.0.0.1:9/v1")
    form.add_tag("beta")
    form.submit.click()
    wait_step(page, 2)
    goto(page, gw, "/ui/")                                   # 只完成 ① 就離開
    banner = page.locator("#resume-banner")
    expect(banner).to_be_visible()
    seen = len(guard_of(page).requests)
    banner.click()
    expect(page).to_have_url(gw.url("/ui/wizard.html"))
    wait_step(page, 3)
    expect(page.locator('#wizard-steps li[data-step="1"]')).to_have_class("done")
    expect(page.locator("#route-tag option")).to_have_text(["beta", "default"])
    expect(page.locator("#route-preview")).to_have_text("符合 1 個成員")
    # 載入時只發 GET(③ 的 tag 預覽是唯一的 POST,不寫入)
    assert {m for m, u in guard_of(page).requests[seen:] if not u.endswith("/api/groups/preview")} == {"GET"}


def test_resume_step4_when_member_and_route_exist(new_page: NewPage, gw: LiveGateway) -> None:
    seed_member(gw, "m1")
    add_route(gw, "review", "chat")
    page = new_page()
    open_wizard(page, gw)
    wait_step(page, 4)
    expect(page.locator("#curl-example")).to_have_attribute("data-route", "review")
    assert {m for m, _ in guard_of(page).requests} == {"GET"}


def test_skip_link(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_wizard(page, gw)
    skip = page.locator("#wizard-skip")
    expect(skip).to_have_text("略過")
    expect(skip).to_have_attribute("href", "/ui/?skip_wizard=1")
    skip.click()
    expect(page).to_have_url(gw.url("/ui/?skip_wizard=1"))
    page.wait_for_selector('#members-table[data-loaded="true"]', state="attached")
    expect(page).to_have_url(gw.url("/ui/?skip_wizard=1"))
    expect(page.locator("main h1")).to_have_text("總覽")

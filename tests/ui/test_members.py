"""UI-2 成員(CHG-20261103-01 T5;決策 12,AM-7;spec 10.2;AC-20、AC-21)。

L2:連動選單(類型 → 供應商 → 模型,內容與 `/api/adapters`、`models.yaml` 一致;「其他(手動輸入)」;
能力與 `model_name` 帶入);adapters 載入中與 `models_error`;不可用模組停用並顯示原因(AC-21,
`make_ui_gateway(extra_adapter_paths=…)`);single 模組已有成員時停用;新增、編輯(PUT 全量)、
啟用切換(先 GET 再 PUT)、刪除(確認對話框)、測試呼叫;送出中按鈕停用;額度;`model_name` 必填;
上游網址 ⇄ `params.api_base`;409 `provider_single`;連線中斷;`params` 非法 JSON;`params` 含金鑰鍵
→ 422;憑證參照 422;`autocomplete="off"`;`members.html#<id>`;`mountMemberForm` 介面(T8 精靈用)。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from playwright.sync_api import Page, Route, expect

from tests.contract.fakemods import write_module
from tests.helpers import TEST_KEY_ENV, dead_base
from tests.helpers.live_gateway import LiveGateway
from tests.helpers.ui import MakeGw, NewPage, api, guard_of, import_module
from tests.helpers.ui_members import (
    BROKEN_PROVIDER,
    MemberForm,
    broken_adapter_dir,
    open_members,
    requests_of,
    seed_member,
)

REPO = Path(__file__).resolve().parents[2]
CLAUDE_MODELS = REPO / "src" / "spillway" / "adapters" / "claude_cli" / "models.yaml"
OTHER_TEXT = "其他(手動輸入)"


@pytest.fixture
def gw(make_ui_gateway: MakeGw) -> LiveGateway:
    return make_ui_gateway()


def new_form(page: Page) -> MemberForm:
    page.locator("#new-member").click()
    form = MemberForm(page)
    form.wait_ready()
    return form


def adapters(gw: LiveGateway) -> list[dict]:
    return api(gw, "GET", "/api/adapters").json()["modules"]


def get_member(gw: LiveGateway, dep_id: str, expect_status: tuple[int, ...] = (200,)) -> dict:
    with gw.http() as c:
        r = c.get(f"/api/deployments/{dep_id}")
    assert r.status_code in expect_status, r.text
    return r.json()


# ---- 連動選單 -------------------------------------------------------------------------------------

def test_cascade_kind_provider_model(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_members(page, gw)
    form = new_form(page)
    mods = adapters(gw)

    form.kind("cli")
    cli_providers = [p for m in mods if m["available"] and "cli" in m["kinds"] for p in m["providers"]]
    assert form.option_values(form.provider, enabled_only=True) == cli_providers
    assert set(cli_providers) == {"claude-cli", "codex-cli"}

    form.provider.select_option("claude-cli")
    entries = yaml.safe_load(CLAUDE_MODELS.read_text(encoding="utf-8"))
    assert form.option_texts(form.model) == [e["label"] for e in entries] + [OTHER_TEXT]
    assert form.option_values(form.model)[:-1] == [e["id"] for e in entries]

    second = entries[1]
    form.model.select_option(second["id"])
    caps = second["capabilities"]
    assert form.caps() == {"ctx": caps["ctx"], "tools": caps["tools"], "vision": caps["vision"],
                           "efforts": caps["efforts"]}
    expect(form.field("model_name")).to_have_value(second["id"])
    expect(form.field("upstream")).to_be_hidden()

    # 其他(手動輸入):能力欄位空白可填,model_name 跟著輸入
    form.model.select_option("__other__")
    expect(form.field("model_manual")).to_be_visible()
    assert form.caps() == {"ctx": None, "tools": False, "vision": False, "efforts": []}
    form.field("model_manual").fill("opus")
    expect(form.field("model_name")).to_have_value("opus")
    form.field("ctx").fill("200000")
    form.form.locator('input[name="effort"][value="high"]').check()
    assert form.caps()["ctx"] == 200000 and form.caps()["efforts"] == ["high"]

    # 換類型 api:供應商只列支援 api 的模組
    form.kind("api")
    api_providers = [p for m in mods if m["available"] and "api" in m["kinds"] for p in m["providers"]]
    assert form.option_values(form.provider, enabled_only=True) == api_providers
    expect(form.field("upstream")).to_be_visible()


def test_adapters_loading_and_models_error(new_page: NewPage, make_ui_gateway: MakeGw,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPILLWAY_LOCAL_API_BASE", dead_base())
    gw = make_ui_gateway()
    page = new_page()
    open_members(page, gw)
    expect(page.locator("#member-table-host .empty")).to_be_visible()     # 清單(含其 adapters 請求)已載入
    held: list[Route] = []
    page.route("**/api/adapters", lambda route: held.append(route))
    page.locator("#new-member").click()
    form = MemberForm(page)
    expect(form.provider).to_contain_text("載入中")
    expect(form.provider).to_be_disabled()
    expect(form.submit).to_be_disabled()
    page.wait_for_function("() => true")      # 讓 route 事件派送
    assert len(held) == 1, "表單的 adapters 請求應被攔住"
    held[0].continue_()
    form.wait_ready()
    form.kind("local")
    form.provider.select_option("litellm")
    err = next(m["models_error"] for m in adapters(gw) if m["module"] == "litellm")
    assert err
    note = form.form.locator('[data-role="models-error"]')
    expect(note).to_be_visible()
    expect(note).to_contain_text(err)


def test_unavailable_module_disabled_ac21(new_page: NewPage, make_ui_gateway: MakeGw, tmp_path: Path) -> None:
    root = broken_adapter_dir(tmp_path / "mods")
    gw = make_ui_gateway(extra_adapter_paths=[root])
    broken = next(m for m in adapters(gw) if m["module"] == "broken")
    assert broken["available"] is False and broken["unavailable_reason"]
    page = new_page()
    open_members(page, gw)
    form = new_form(page)
    for kind in ("api", "cli", "local"):
        form.kind(kind)
        opt = form.provider.locator(f'option[value="{BROKEN_PROVIDER}"]')
        expect(opt).to_have_count(1)
        expect(opt).to_be_disabled()
        expect(opt).to_contain_text(broken["unavailable_reason"])


def test_single_module_with_member_disabled(new_page: NewPage, gw: LiveGateway) -> None:
    seed_member(gw, "claude-sub", kind="cli", provider="claude-cli", model="claude-sonnet-5-5",
                params={}, credential_ref=None)
    page = new_page()
    open_members(page, gw)
    form = new_form(page)
    form.kind("cli")
    opt = form.provider.locator('option[value="claude-cli"]')
    expect(opt).to_be_disabled()
    expect(opt).to_contain_text("已有 1 個,僅限 1 個")
    expect(form.provider.locator('option[value="codex-cli"]')).to_be_enabled()
    expect(form.provider).to_have_value("codex-cli")
    # 編輯該成員本身時,自己的供應商可選
    open_members(page, gw, "claude-sub")
    form = MemberForm(page)
    form.wait_ready()
    expect(form.provider).to_have_value("claude-cli")
    expect(form.provider.locator('option[value="claude-cli"]')).to_be_enabled()
    expect(form.model).to_have_value("claude-sonnet-5-5")


@pytest.mark.expect_http_errors(409)
def test_provider_single_409_message(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_members(page, gw)
    form = new_form(page)
    form.kind("cli")
    form.provider.select_option("claude-cli")
    form.field("id").fill("claude-2")
    # 表單開啟後,另一處先建立了 claude-cli 成員
    seed_member(gw, "claude-1", kind="cli", provider="claude-cli", model="claude-sonnet-5-5",
                params={}, credential_ref=None)
    form.submit.click()
    expect(page.locator("#msgbar")).to_contain_text("僅允許 1 個成員")
    assert get_member(gw, "claude-2", (404,))


# ---- 新增、編輯、測試呼叫、額度 -------------------------------------------------------------------

def test_create_edit_test_call_and_budget(new_page: NewPage, gw: LiveGateway, fake_api) -> None:  # noqa: ANN001
    page = new_page()
    open_members(page, gw)
    form = new_form(page)
    form.kind("api")
    form.provider.select_option("openai")
    form.model.select_option("gpt-5.5")
    form.field("id").fill("openai-1")
    form.field("upstream").fill(f"{fake_api.base_url}/v1")
    form.field("credential_ref").fill(f"env:{TEST_KEY_ENV}")
    form.set_budget("usd", "10", "monthly", "")
    form.add_tag("coding")
    posts: list[dict] = []
    page.on("request", lambda r: posts.append(r.post_data_json)
            if r.method == "POST" and r.url.endswith("/api/deployments") else None)
    form.submit.click()
    expect(page.locator("#editor-title")).to_contain_text("openai-1")
    dep = get_member(gw, "openai-1")
    assert dep["budget"] == {"type": "usd", "limit_amount": 10, "window": "monthly", "reset_anchor": None}
    assert dep["params"] == {"api_base": f"{fake_api.base_url}/v1"}
    assert dep["model"] == "gpt-5.5" and dep["model_name"] == "gpt-5.5" and dep["provider"] == "openai"
    assert dep["tags"] == ["coding"] and dep["credential_ref"] == f"env:{TEST_KEY_ENV}"
    openai_caps = next(x for m in adapters(gw) if m["module"] == "openai_api" for x in m["models"]
                       if x["id"] == "gpt-5.5")["capabilities"]
    assert dep["capabilities"] == openai_caps
    assert len(posts) == 1
    dash = next(d for d in api(gw, "GET", "/api/dashboard").json()["deployments"] if d["id"] == "openai-1")
    assert dash["remaining"] == 10 and dash["unlimited"] is False
    row = page.locator('tr[data-member="openai-1"]')
    expect(row).to_contain_text("$10.00")
    expect(row).to_contain_text("每月")

    # 編輯:PUT 全量;送出中按鈕停用
    form = MemberForm(page)
    form.wait_ready()
    form.field("model_name").fill("gpt")
    held: list[Route] = []
    page.route("**/api/deployments/openai-1", lambda route: held.append(route)
               if route.request.method == "PUT" else route.continue_())
    form.submit.click()
    expect(form.submit).to_be_disabled()
    page.wait_for_function("() => true")
    assert len(held) == 1
    put = held[0].request.post_data_json
    assert set(put) == {"id", "model_name", "kind", "provider", "model", "params", "credential_ref",
                        "capabilities", "enabled", "tags", "budget"}
    assert put["model_name"] == "gpt" and put["budget"]["limit_amount"] == 10
    held[0].continue_()
    expect(page.locator("#msgbar")).to_contain_text("已儲存成員")
    assert get_member(gw, "openai-1")["model_name"] == "gpt"

    # 測試呼叫:status、延遲、tokens
    btn = page.locator('#editor-actions [data-role="test"]')
    with page.expect_response(lambda r: r.url.endswith("/api/deployments/openai-1/test")) as resp:
        btn.click()
    result = resp.value.json()
    assert result["status"] == "ok" and result["tokens_in"] is not None
    summary = page.locator('#test-result [data-role="test-summary"]')
    expect(summary).to_contain_text("成功")
    expect(summary).to_contain_text(f"延遲 {result['latency_ms']} ms")
    expect(summary).to_contain_text(f"輸入 {result['tokens_in']} / 輸出 {result['tokens_out']}")
    ledger = api(gw, "GET", "/api/ledger").json()["items"]
    assert [r["deployment_id"] for r in ledger] == ["openai-1"]


def test_budget_none_sends_null_and_anchor(new_page: NewPage, gw: LiveGateway) -> None:
    seed_member(gw, "m1", budget={"type": "usd", "limit_amount": 5, "window": "monthly", "reset_anchor": 3})
    page = new_page()
    open_members(page, gw, "m1")
    form = MemberForm(page)
    form.wait_ready()
    expect(form.field("reset_anchor")).to_have_value("3")
    form.field("window").select_option("weekly")
    expect(form.field("reset_anchor")).to_be_hidden()
    form.field("budget_type").select_option("none")
    expect(form.field("limit_amount")).to_be_hidden()
    with page.expect_request(lambda r: r.method == "PUT" and r.url.endswith("/api/deployments/m1")) as req:
        form.submit.click()
    assert req.value.post_data_json["budget"] is None
    expect(page.locator("#msgbar")).to_contain_text("已儲存成員")
    assert get_member(gw, "m1")["budget"] is None


# ---- 用戶端驗證 -----------------------------------------------------------------------------------

def test_model_name_required_and_params_json(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_members(page, gw)
    form = new_form(page)
    form.field("id").fill("x1")
    form.field("model_name").fill("")
    guard = guard_of(page)
    form.submit.click()
    expect(form.error).to_have_text("model_name 為必填")
    expect(form.field("model_name")).to_have_attribute("aria-invalid", "true")
    form.field("model_name").fill("x1")
    form.field("params").fill('{"timeout": 30,')
    form.submit.click()
    expect(form.error).to_have_text("params 必須是 JSON 物件")
    form.field("params").fill("[1, 2]")
    form.submit.click()
    expect(form.error).to_have_text("params 必須是 JSON 物件")
    assert requests_of(guard, "POST", "/api/deployments") == []
    get_member(gw, "x1", (404,))


def test_upstream_params_sync(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_members(page, gw)
    form = new_form(page)
    form.kind("api")
    form.field("upstream").fill("http://127.0.0.1:1234/v1")
    assert json.loads(form.field("params").input_value()) == {"api_base": "http://127.0.0.1:1234/v1"}
    form.field("params").fill('{"api_base": "http://127.0.0.1:5678/v1", "timeout": 60}')
    expect(form.field("upstream")).to_have_value("http://127.0.0.1:5678/v1")
    form.field("upstream").fill("")
    assert json.loads(form.field("params").input_value()) == {"timeout": 60}
    form.kind("local")
    form.field("id").fill("loc1")
    form.submit.click()
    expect(form.error).to_have_text("Local 成員必須填寫上游網址")
    form.kind("cli")
    expect(form.field("upstream")).to_be_hidden()


def test_autocomplete_off(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_members(page, gw)
    form = new_form(page)
    expect(form.field("params")).to_have_attribute("autocomplete", "off")
    expect(form.field("credential_ref")).to_have_attribute("autocomplete", "off")
    expect(form.form).to_contain_text("填環境變數名或 keyring 項目")
    expect(form.form).to_contain_text("不要填金鑰本身")
    assert form.form.locator('input[type="password"]').count() == 0
    for key in ("effort_args", "extra_args", "max_concurrency", "timeout"):
        expect(form.form.locator(".params-keys")).to_contain_text(key)


@pytest.mark.expect_http_errors(422)
def test_server_rejections_shown(new_page: NewPage, gw: LiveGateway) -> None:
    secret = "sk-ant-api03-ZZZZZZZZZZZZZZZZZZZZZZZZ"
    page = new_page()
    open_members(page, gw)
    form = new_form(page)
    form.field("id").fill("leaky")
    form.field("params").fill(json.dumps({"api_key": secret}))
    form.submit.click()
    bar = page.locator("#msgbar")
    expect(bar).to_contain_text("credential_ref")
    assert secret not in bar.inner_text()
    get_member(gw, "leaky", (404,))
    form.field("params").fill("{}")
    form.field("credential_ref").fill("plain-value")
    form.submit.click()
    expect(bar).to_contain_text("env:變數名")
    get_member(gw, "leaky", (404,))


# ---- 清單:hash、啟用切換、刪除、連線中斷 ---------------------------------------------------------

def test_hash_opens_member_form(new_page: NewPage, gw: LiveGateway) -> None:
    seed_member(gw, "alpha")
    seed_member(gw, "beta", model_name="b-name", tags=["x", "y"])
    page = new_page()
    open_members(page, gw, "beta")
    expect(page.locator("#editor-title")).to_have_text("編輯 beta")
    form = MemberForm(page)
    form.wait_ready()
    expect(form.field("id")).to_have_value("beta")
    expect(form.field("id")).to_be_disabled()
    expect(form.field("model_name")).to_have_value("b-name")
    expect(form.form.locator('[data-role="tags"] .chip')).to_have_count(2)
    # hashchange 也會開啟
    page.locator('tr[data-member="alpha"] a').click()
    expect(page.locator("#editor-title")).to_have_text("編輯 alpha")
    expect(page.locator('tr[data-member="alpha"]')).to_have_attribute("aria-selected", "true")


def test_hash_unknown_member_no_request(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_members(page, gw, "nope")
    expect(page.locator("#msgbar")).to_contain_text("找不到成員:nope")
    expect(page.locator("#member-editor")).to_be_hidden()
    assert requests_of(guard_of(page), "GET", "/api/deployments/nope") == []


def test_toggle_get_then_put(new_page: NewPage, gw: LiveGateway) -> None:
    seed_member(gw, "t1")
    page = new_page()
    open_members(page, gw)
    box = page.locator('input[data-toggle="t1"]')
    expect(box).to_be_checked()
    # 清單載入後,別處修改了成員:切換不可用清單的舊資料覆寫
    fresh = get_member(gw, "t1")
    body = {k: fresh[k] for k in ("id", "model_name", "kind", "provider", "model", "params", "credential_ref",
                                  "capabilities", "enabled", "tags", "budget")}
    body["model_name"] = "changed"
    api(gw, "PUT", "/api/deployments/t1", body)
    seen: list[tuple[str, str]] = []
    page.on("request", lambda r: seen.append((r.method, r.url.split("/api/", 1)[-1]))
            if "/api/deployments/t1" in r.url else None)
    with page.expect_request(lambda r: r.method == "PUT") as put:
        box.uncheck()
    assert seen[:2] == [("GET", "deployments/t1"), ("PUT", "deployments/t1")]
    sent = put.value.post_data_json
    assert sent["enabled"] is False and sent["model_name"] == "changed"
    expect(page.locator("#msgbar")).to_contain_text("已停用 t1")
    after = get_member(gw, "t1")
    assert after["enabled"] is False and after["model_name"] == "changed"
    expect(page.locator('input[data-toggle="t1"]')).not_to_be_checked()


def test_delete_with_confirm(new_page: NewPage, gw: LiveGateway) -> None:
    seed_member(gw, "d1", tags=["coding", "cheap"])
    page = new_page()
    open_members(page, gw, "d1")
    page.locator('#editor-actions [data-role="delete"]').click()
    dialog = page.locator("dialog")
    expect(dialog).to_contain_text("cheap, coding")
    expect(dialog).to_contain_text("額度")
    expect(dialog).to_contain_text("route 排序")
    expect(dialog).to_contain_text("帳本紀錄保留")
    dialog.locator('[data-role="cancel"]').click()
    expect(dialog).to_have_count(0)
    assert get_member(gw, "d1")["id"] == "d1"
    page.locator('#editor-actions [data-role="delete"]').click()
    page.locator('dialog [data-role="confirm"]').click()
    expect(page.locator("#msgbar")).to_contain_text("已刪除成員 d1")
    expect(page.locator("#member-editor")).to_be_hidden()
    expect(page.locator('tr[data-member="d1"]')).to_have_count(0)
    get_member(gw, "d1", (404,))


def test_network_error_message(new_page: NewPage, gw: LiveGateway) -> None:
    seed_member(gw, "n1")
    page = new_page()
    open_members(page, gw)
    box = page.locator('input[data-toggle="n1"]')
    expect(box).to_be_checked()
    gw.stop()
    box.uncheck()
    expect(page.locator("#msgbar")).to_have_text("無法連線到 spillway×")
    expect(box).to_be_checked()
    guard_of(page).take(lambda v: v.kind == "console" and "net::ERR_CONNECTION_REFUSED" in v.text)


# ---- mountMemberForm(T8 精靈使用的介面)---------------------------------------------------------

def test_mount_member_form_contract(new_page: NewPage, gw: LiveGateway, fake_api) -> None:  # noqa: ANN001
    page = new_page()
    open_members(page, gw)
    assert "mountMemberForm" in import_module(page, "member-form.js")
    page.evaluate("""() => {
        const host = document.createElement("div");
        host.id = "wizard-host";
        document.querySelector("main").append(host);
        window.__saved = [];
        window.__handle = window.__mods["member-form.js"].mountMemberForm(host, {
            initial: null, defaults: {kind: "api", tags: ["default"]}, requireTag: true,
            submitLabel: "下一步", onSaved: (m, info) => { window.__saved.push([m.id, info.created]); },
        });
    }""")
    form = MemberForm(page, page.locator("#wizard-host"))
    form.wait_ready()
    expect(form.submit).to_have_text("下一步")
    expect(form.form.locator('button[data-kind="api"]')).to_have_attribute("aria-pressed", "true")
    expect(form.form.locator('[data-role="tags"] .chip')).to_have_text(["default×"])
    form.form.locator('button[aria-label="移除 default"]').click()
    form.field("id").fill("w1")
    form.field("upstream").fill(f"{fake_api.base_url}/v1")
    form.submit.click()
    expect(form.error).to_have_text("至少需要 1 個 tag")
    assert requests_of(guard_of(page), "POST", "/api/deployments") == []
    form.add_tag("default")
    form.submit.click()
    page.wait_for_function("() => window.__saved.length === 1")
    assert page.evaluate("() => window.__saved") == [["w1", True]]
    assert get_member(gw, "w1")["tags"] == ["default"]
    page.evaluate("() => window.__handle.destroy()")
    expect(page.locator("#wizard-host form")).to_have_count(0)


# ---- 施工審議 T5 修正(M1–M6、選修)----------------------------------------------------------------

SYNC_NOTE = "params.api_base 與上游網址不同,已以上游網址為準"


@pytest.mark.parametrize("kind", ["api", "local"])
def test_upstream_kept_when_params_fixed(new_page: NewPage, gw: LiveGateway, kind: str) -> None:
    up = "http://127.0.0.1:4321/v1"
    page = new_page()
    open_members(page, gw)
    form = new_form(page)
    form.kind(kind)
    form.field("id").fill(f"{kind}-up")
    form.field("params").fill('{"timeout": 60')
    form.field("upstream").fill(up)
    form.field("params").fill('{"timeout": 60}')
    expect(form.field("upstream")).to_have_value(up)
    assert json.loads(form.field("params").input_value()) == {"timeout": 60, "api_base": up}
    form.submit.click()
    expect(page.locator("#editor-title")).to_contain_text(f"{kind}-up")
    assert get_member(gw, f"{kind}-up")["params"] == {"timeout": 60, "api_base": up}


def test_upstream_conflict_upstream_wins_with_note(new_page: NewPage, gw: LiveGateway) -> None:
    a, b = "http://127.0.0.1:1111/v1", "http://127.0.0.1:2222/v1"
    page = new_page()
    open_members(page, gw)
    form = new_form(page)
    form.kind("api")
    form.field("id").fill("conflict")
    form.field("params").fill("{")
    form.field("upstream").fill(b)
    form.field("params").fill(json.dumps({"api_base": a}))
    expect(form.field("upstream")).to_have_value(b)
    expect(form.form.locator('[data-role="form-note"]')).to_have_text(SYNC_NOTE)
    assert json.loads(form.field("params").input_value()) == {"api_base": b}
    form.submit.click()
    expect(page.locator("#msgbar")).to_contain_text(SYNC_NOTE)
    assert get_member(gw, "conflict")["params"]["api_base"] == b


@pytest.mark.parametrize("field,error", [("limit_amount", "上限必須是 0 以上的數字"),
                                         ("ctx", "Context 上限必須是正整數")])
def test_bad_number_input_not_sent(new_page: NewPage, gw: LiveGateway, field: str, error: str) -> None:
    budget = {"type": "usd", "limit_amount": 5, "window": "monthly", "reset_anchor": None}
    seed_member(gw, "num1", budget=budget)
    page = new_page()
    open_members(page, gw, "num1")
    form = MemberForm(page)
    form.wait_ready()
    form.field(field).fill("")
    form.field(field).press_sequentially("1e")
    assert form.field(field).evaluate("(el) => el.validity.badInput") is True
    guard = guard_of(page)
    form.submit.click()
    expect(form.error).to_have_text(error)
    assert requests_of(guard, "PUT", "/api/deployments/num1") == []
    after = get_member(gw, "num1")
    assert after["budget"] == budget and after["capabilities"]["ctx"] == 1000


def test_toggle_updates_open_editor(new_page: NewPage, gw: LiveGateway) -> None:
    seed_member(gw, "m1")
    page = new_page()
    open_members(page, gw, "m1")
    form = MemberForm(page)
    form.wait_ready()
    expect(form.field("enabled")).to_be_checked()
    page.locator('input[data-toggle="m1"]').uncheck()
    expect(page.locator("#msgbar")).to_contain_text("已停用 m1")
    expect(form.field("enabled")).not_to_be_checked()
    form.submit.click()
    expect(page.locator("#msgbar")).to_contain_text("已儲存成員")
    assert get_member(gw, "m1")["enabled"] is False


def test_dot_ids_rejected_on_create(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_members(page, gw)
    form = new_form(page)
    for bad in (".", ".."):
        form.field("id").fill(bad)
        form.submit.click()
        expect(form.error).to_contain_text("不可為 . 或 ..")
    assert requests_of(guard_of(page), "POST", "/api/deployments") == []


def test_dot_id_member_not_managed(new_page: NewPage, gw: LiveGateway) -> None:
    seed_member(gw, ".")
    seed_member(gw, "ok1")
    page = new_page()
    open_members(page, gw)
    row = page.locator('tr[data-member="."]')
    expect(row).to_be_visible()
    expect(row.locator("a")).to_have_count(0)
    expect(page.locator('input[data-toggle="."]')).to_be_disabled()
    expect(page.locator('input[data-toggle="ok1"]')).to_be_enabled()
    guard = guard_of(page)
    guard.requests.clear()
    page.goto(gw.url("/ui/members.html#."))
    expect(page.locator("#msgbar")).to_contain_text("無法在畫面上管理")
    expect(page.locator("#member-editor")).to_be_hidden()
    assert [u for _, u in guard.requests if "/api/deployments/" in u] == []


def test_litellm_member_create_and_edit(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_members(page, gw)
    form = new_form(page)
    form.kind("api")
    form.provider.select_option("litellm")
    expect(form.field("llm_provider")).to_be_visible()
    form.model.select_option(label="DeepSeek Chat")
    expect(form.field("llm_provider")).to_have_value("deepseek")
    form.field("llm_provider").fill("deepseek")
    form.field("id").fill("ds1")
    form.submit.click()
    expect(page.locator("#editor-title")).to_contain_text("ds1")
    dep = get_member(gw, "ds1")
    assert (dep["provider"], dep["model"], dep["model_name"]) == ("deepseek", "deepseek-chat", "deepseek-chat")

    seed_member(gw, "ds2", provider="deepseek", model="deepseek-chat", model_name="ds")
    before = get_member(gw, "ds2")
    open_members(page, gw, "ds2")
    form = MemberForm(page)
    form.wait_ready()
    expect(form.provider).to_have_value("litellm")
    expect(form.field("llm_provider")).to_have_value("deepseek")
    expect(form.model.locator("option:checked")).to_have_text("DeepSeek Chat")
    form.submit.click()
    expect(page.locator("#msgbar")).to_contain_text("已儲存成員")
    assert get_member(gw, "ds2") == before


def test_removed_cli_module_member(new_page: NewPage, gw: LiveGateway) -> None:
    seed_member(gw, "gone", kind="cli", provider="gone-cli", model="x-1", params={}, credential_ref=None)
    page = new_page()
    open_members(page, gw)
    expect(page.locator('tr[data-member="gone"]')).to_contain_text("模組不可用")
    open_members(page, gw, "gone")
    form = MemberForm(page)
    form.wait_ready()
    expect(form.provider).to_have_value("gone-cli")
    expect(form.provider.locator("option:checked")).to_have_text("gone-cli(模組不可用)")
    expect(form.model).to_have_value("__other__")
    expect(form.field("model_manual")).to_have_value("x-1")


def test_mount_member_form_edit_and_cancel(new_page: NewPage, gw: LiveGateway) -> None:
    seed_member(gw, "e1")
    page = new_page()
    open_members(page, gw)
    import_module(page, "member-form.js")
    page.evaluate("""async () => {
        const initial = await (await fetch("/api/deployments/e1", {cache: "no-store"})).json();
        const host = document.createElement("div");
        host.id = "edit-host";
        document.querySelector("main").append(host);
        window.__saved = [];
        window.__cancelled = 0;
        window.__handle = window.__mods["member-form.js"].mountMemberForm(host, {
            initial, onSaved: (m, info) => { window.__saved.push([m.id, info.created]); },
            onCancel: () => { window.__cancelled += 1; },
        });
    }""")
    form = MemberForm(page, page.locator("#edit-host"))
    form.wait_ready()
    expect(form.field("id")).to_be_disabled()
    form.field("model_name").fill("e1-renamed")
    form.submit.click()
    page.wait_for_function("() => window.__saved.length === 1")
    assert page.evaluate("() => window.__saved") == [["e1", False]]
    assert get_member(gw, "e1")["model_name"] == "e1-renamed"
    form.form.locator('[data-role="cancel"]').click()
    assert page.evaluate("() => window.__cancelled") == 1


def test_user_data_rendered_as_text(new_page: NewPage, make_ui_gateway: MakeGw, tmp_path: Path) -> None:
    evil = '<img src=x onerror="window.__xss=1">'
    write_module(tmp_path / "mods", "xssmod", providers=("xss-p",),
                 models_yaml=("- id: xm\n  label: '" + evil + "'\n"
                              "  capabilities: {ctx: 10, tools: false, vision: false, efforts: []}\n"))
    gw = make_ui_gateway(extra_adapter_paths=[tmp_path / "mods"])
    seed_member(gw, "<i>x", tags=[evil])
    page = new_page()
    open_members(page, gw)
    row = page.locator("#member-table-host tbody tr").first
    expect(row).to_contain_text("<i>x")
    expect(row).to_contain_text(evil)
    form = new_form(page)
    form.provider.select_option("xss-p")
    expect(form.model.locator("option").first).to_have_text(evil)
    assert page.locator("main img, main i").count() == 0
    assert page.evaluate("() => window.__xss") is None

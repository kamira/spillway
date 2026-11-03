"""UI-7 設定(CHG-20261103-01 T8 L2;決策 17,決策審議修訂 AM-9、AM-12、AM-13、AM-15;設計差異 #9)。

總覽欄位等於 `GET /api/settings`(token 只顯示已產生/未產生)、`spillway serve --port <N>` 可複製、頁首精靈連結;
context 三欄存檔(KB × 1024)、0 天說明、小於 1 KB 的 422;匯出下載;匯入預覽流程(只有「預覽匯入」送 dry_run、
確認對話框計數與 privileged、replace 的 danger 與備份、取消不寫入、送出同一份 bytes、改檔案或 mode 作廢預覽、
422 列出全部錯誤、409、> 1 MiB 不送出);token 對話框(首次與重新產生的文字、顯示一次、雜湊相符、export 範例、
關閉與 pagehide 後 DOM/input 不含 token、返回與前進後也不含、未複製時再提示)。
"""
from __future__ import annotations

import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from playwright.sync_api import Locator, Page, Request, Route, expect

from tests.helpers.live_gateway import LiveGateway
from tests.helpers.ui import MakeGw, NewPage, api, db_execute, db_snapshot, goto, guard_of, import_module
from tests.helpers.ui_members import seed_member
from tests.helpers.ui_settings import (
    choose_import,
    export_yaml,
    import_dialog,
    import_requests,
    open_settings,
    page_leaks,
    token_hash,
)

CLIP = ["clipboard-read", "clipboard-write"]


@pytest.fixture
def gw(make_ui_gateway: MakeGw) -> LiveGateway:
    return make_ui_gateway()


def sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


# ---- ① 總覽 -----------------------------------------------------------------------------------------

def test_overview_matches_api_and_copy_serve(new_page: NewPage, gw: LiveGateway) -> None:
    s = api(gw, "GET", "/api/settings").json()
    page = new_page(permissions=CLIP)
    open_settings(page, gw)
    expect(page.locator("#ov-version")).to_have_text(s["version"])
    expect(page.locator("#ov-data-dir")).to_have_text(s["data_dir"])
    expect(page.locator("#ov-port")).to_have_text(str(s["port"]))
    assert s["token_issued"] is True
    expect(page.locator("#ov-token")).to_have_text("已產生")
    expect(page.locator("#token-status")).to_have_text("已產生")
    cmd = f"spillway serve --port {s['port']}"
    expect(page.locator("#serve-cmd")).to_have_text(cmd)
    page.locator("#serve-copy").click()
    expect(page.locator("#msgbar")).to_contain_text("已複製")
    assert page.evaluate("navigator.clipboard.readText()") == cmd
    # 頁首的精靈連結
    link = page.locator("main .head #wizard-link")
    expect(link).to_have_text("開啟首次設定精靈")
    expect(link).to_have_attribute("href", "/ui/wizard.html")
    # 埠號唯讀:畫面上沒有任何可編輯的埠號或綁定位址欄位
    names = page.locator("input, select, textarea").evaluate_all("(els) => els.map((e) => e.name || e.id)")
    assert [n for n in names if any(w in n for w in ("port", "host", "bind")) and not n.startswith("import")] == []
    assert {m for m, _ in guard_of(page).requests} == {"GET"}
    assert gw.token and page_leaks(page, gw.token) == []


def test_overview_token_not_issued(new_page: NewPage, make_ui_gateway: MakeGw) -> None:
    gw = make_ui_gateway(token=False)
    page = new_page()
    open_settings(page, gw, "#token")
    expect(page.locator("#ov-token")).to_have_text("未產生")
    expect(page.locator("#token-status")).to_have_text("未產生")
    expect(page.locator("#token")).to_be_in_viewport()
    assert {m for m, _ in guard_of(page).requests} == {"GET"}     # hash 不觸發非 GET 請求


# ---- ② context ---------------------------------------------------------------------------------------

def test_context_form_roundtrip_kb(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_settings(page, gw)
    cur = api(gw, "GET", "/api/settings/context").json()
    expect(page.locator("#ctx-record")).to_be_checked(checked=cur["record_context_default"])
    expect(page.locator("#ctx-days")).to_have_value(str(cur["context_retention_days"]))
    expect(page.locator("#ctx-kb")).to_have_value(str(cur["context_max_bytes"] // 1024))
    expect(page.locator("#ctx-stats")).to_have_text("目前 0 筆 · 0 B")

    # KB 換算函式:1 KB = 1024 bytes
    import_module(page, "settings.js")
    assert page.evaluate("""() => { const m = window.__mods["settings.js"];
        return [m.KB, m.kbToBytes(1), m.kbToBytes(2.5), m.bytesToKb(1024), m.bytesToKb(65536)]; }""") \
        == [1024, 1024, 2560, 1, 64]

    expect(page.locator("#ctx-days-zero")).to_be_hidden()
    page.locator("#ctx-record").set_checked(not cur["record_context_default"])
    page.locator("#ctx-days").fill("00")
    expect(page.locator("#ctx-days-zero")).to_be_visible()
    page.locator("#ctx-days").fill("0")
    expect(page.locator("#ctx-days-zero")).to_be_visible()
    expect(page.locator("#ctx-days-zero")).to_contain_text("0 表示不保存內容")
    page.locator("#ctx-kb").fill("3")
    with page.expect_request(lambda r: r.method == "PUT" and r.url.endswith("/api/settings/context")) as req:
        page.locator("#ctx-save").click()
    assert req.value.post_data_json == {"record_context_default": not cur["record_context_default"],
                                        "context_retention_days": 0, "context_max_bytes": 3 * 1024}
    expect(page.locator("#msgbar")).to_contain_text("已儲存 context 設定")
    assert api(gw, "GET", "/api/settings/context").json() == {
        "record_context_default": not cur["record_context_default"], "context_retention_days": 0,
        "context_max_bytes": 3072}
    page.reload()
    page.wait_for_selector('#context-form[data-loaded="true"]', state="attached")
    expect(page.locator("#ctx-kb")).to_have_value("3")
    expect(page.locator("#ctx-days-zero")).to_be_visible()


@pytest.mark.expect_http_errors(422)
def test_context_below_1kb_shows_422(new_page: NewPage, gw: LiveGateway) -> None:
    before = api(gw, "GET", "/api/settings/context").json()
    page = new_page()
    open_settings(page, gw)
    page.locator("#ctx-kb").fill("0.5")
    with page.expect_response(lambda r: r.url.endswith("/api/settings/context") and r.request.method == "PUT") as rr:
        page.locator("#ctx-save").click()
    assert rr.value.status == 422
    message = rr.value.json()["error"]["message"]
    expect(page.locator("#ctx-error")).to_have_text(message)
    expect(page.locator("#ctx-error")).to_have_class("callout err")
    expect(page.locator("#msgbar")).to_contain_text(message)
    assert "context_max_bytes" in message
    assert api(gw, "GET", "/api/settings/context").json() == before


def test_context_client_validation(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_settings(page, gw)
    page.locator("#ctx-days").fill("1.5")
    page.locator("#ctx-save").click()
    expect(page.locator("#ctx-error")).to_have_text("保存天數必須是 0 以上的整數")
    page.locator("#ctx-days").fill("7")
    page.locator("#ctx-kb").fill("")
    page.locator("#ctx-save").click()
    expect(page.locator("#ctx-error")).to_have_text("單筆上限必須是數字(KB)")
    assert [m for m, _ in guard_of(page).requests if m != "GET"] == []


# ---- ③ 匯出 ------------------------------------------------------------------------------------------

def test_export_download_matches_api(new_page: NewPage, gw: LiveGateway) -> None:
    seed_member(gw, "api-a")
    page = new_page()
    open_settings(page, gw)
    with page.expect_download() as dl:
        page.locator("#export-link").click()
    download = dl.value
    assert download.suggested_filename.startswith("spillway-config-")
    assert download.suggested_filename.endswith(".yaml")
    with open(download.path(), "rb") as fh:
        data = fh.read()
    assert data == export_yaml(gw).encode("utf-8")


@pytest.mark.expect_http_errors(422)
def test_export_422_shows_message_without_download(new_page: NewPage, gw: LiveGateway) -> None:
    seed_member(gw, "api-a")
    db_execute(gw, "UPDATE deployments SET params = ? WHERE id = ?", ('{"api_key": "x"}', "api-a"))
    page = new_page()
    open_settings(page, gw)
    downloads: list = []
    page.on("download", lambda d: downloads.append(d))
    with page.expect_response(lambda r: r.url.endswith("/api/config/export")) as rr:
        page.locator("#export-link").click()
    assert rr.value.status == 422
    message = rr.value.json()["error"]["message"]
    expect(page.locator("#msgbar")).to_contain_text(message)
    assert "deployments[api-a].params" in message
    page.wait_for_function("() => true")
    assert downloads == []


# ---- ④ 匯入 ------------------------------------------------------------------------------------------

def changed_config(gw: LiveGateway, dep_id: str, new_base: str) -> bytes:
    doc = yaml.safe_load(export_yaml(gw))
    for dep in doc["deployments"]:
        if dep["id"] == dep_id:
            dep["params"]["api_base"] = new_base
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False).encode("utf-8")


def dry_run(gw: LiveGateway, data: bytes, mode: str) -> dict:
    with gw.http() as c:
        r = c.post(f"/api/config/import?mode={mode}&dry_run=true", content=data,
                   headers={"Content-Type": "application/yaml"})
    assert r.status_code == 200, r.text
    return r.json()


def record_import_bodies(page: Page) -> list[tuple[str, bytes]]:
    bodies: list[tuple[str, bytes]] = []

    def on_request(r: Request) -> None:
        if r.method == "POST" and "/api/config/import" in r.url:
            bodies.append((r.url, r.post_data_buffer or b""))

    page.on("request", on_request)
    return bodies


def test_import_replace_preview_cancel_then_import(new_page: NewPage, gw: LiveGateway) -> None:
    seed_member(gw, "api-a")
    seed_member(gw, "api-b", params={"api_base": "http://127.0.0.1:9/v1"})
    data = changed_config(gw, "api-b", "http://127.0.0.1:10/v1")
    seed_member(gw, "api-c")                              # 不在檔案中 → replace 會刪除
    expected = dry_run(gw, data, "replace")
    assert expected["deleted"]["deployments"] == 1 and expected["privileged"] == ["api-b"]
    page = new_page()
    open_settings(page, gw)
    bodies = record_import_bodies(page)
    guard = guard_of(page)

    choose_import(page, data, mode="replace")
    page.locator('input[name="import_mode"][value="merge"]').check()
    page.locator('input[name="import_mode"][value="replace"]').check()
    expect(page.locator("#import-preview")).to_be_enabled()
    assert import_requests(guard) == []                 # 選檔與 mode 不送請求

    snap = db_snapshot(gw)
    page.locator("#import-preview").click()
    dialog = import_dialog(page)
    expect(dialog).to_be_visible()
    assert [query(u) for u in import_requests(guard)] == [{"mode": ["replace"], "dry_run": ["true"]}]
    expect(dialog).to_have_class("dialog dialog-danger")
    expect(dialog.locator(".dialog-title")).to_have_text("確認匯入(replace)")
    expect(dialog.locator('[data-role="confirm"]')).to_have_class("btn danger")
    expect(dialog.locator('[data-role="confirm"]')).to_have_text("匯入")
    expect(dialog.locator('[data-role="cancel"]')).to_be_focused()       # 預設焦點在「取消」
    for key in ("created", "updated", "deleted"):
        for kind in ("deployments", "groups", "routes"):
            expect(dialog.locator(f'[data-count="{kind}.{key}"]')).to_have_text(str(expected[key][kind]))
    expect(dialog.locator('[data-count="deployments.deleted"]')).to_have_class("num danger-text")   # 警示樣式
    expect(dialog.locator('[data-count="groups.deleted"]')).to_have_class("num")
    expect(dialog.locator('[data-role="privileged"]')).to_have_text("api-b")
    expect(dialog.locator('[data-role="privileged-note"]')).to_contain_text("憑證參照、上游網址、供應商或 CLI 參數會改變")
    expect(dialog.locator('[data-role="backup-note"]')).to_have_text("會先備份到 backups/。")
    assert db_snapshot(gw) == snap                       # dry_run 不寫入

    dialog.locator('[data-role="cancel"]').click()
    expect(dialog).to_have_count(0)
    assert db_snapshot(gw) == snap
    assert all(query(u).get("dry_run") == ["true"] for u in import_requests(guard))
    expect(page.locator("#import-result")).to_be_hidden()

    # 再次預覽 → 匯入:送出的 bytes 與 dry_run 相同
    page.locator("#import-preview").click()
    expect(dialog).to_be_visible()
    with page.expect_response(lambda r: "/api/config/import" in r.url and "dry_run" not in r.url) as rr:
        dialog.locator('[data-role="confirm"]').click()
    result = rr.value.json()
    assert result["mode"] == "replace" and result["dry_run"] is False and result["backup"]
    assert [query(u) for u, _ in bodies] == [{"mode": ["replace"], "dry_run": ["true"]}] * 2 + [{"mode": ["replace"]}]
    assert {b for _, b in bodies} == {data}
    expect(page.locator('#import-result [data-role="backup"]')).to_have_text(f"backups/{result['backup']}")
    assert (gw.db_path.parent / "backups" / result["backup"]).is_file()
    dep = api(gw, "GET", "/api/deployments/api-b").json()
    assert dep["params"]["api_base"] == "http://127.0.0.1:10/v1"


def test_import_merge_no_backup_and_escape_cancels(new_page: NewPage, gw: LiveGateway) -> None:
    seed_member(gw, "api-a")
    data = changed_config(gw, "api-a", "http://127.0.0.1:11/v1")
    page = new_page()
    open_settings(page, gw)
    guard = guard_of(page)
    choose_import(page, data, mode="merge")
    page.locator("#import-preview").click()
    dialog = import_dialog(page)
    expect(dialog).to_be_visible()
    expect(dialog).to_have_class("dialog")
    expect(dialog.locator(".dialog-title")).to_have_text("確認匯入(merge)")
    expect(dialog.locator('[data-role="confirm"]')).to_have_class("btn primary")
    expect(dialog.locator('[data-role="backup-note"]')).to_have_count(0)
    expect(dialog.locator('[data-count="deployments.updated"]')).to_have_text("1")
    expect(dialog.locator('[data-count="deployments.deleted"]')).to_have_class("num")
    page.keyboard.press("Escape")                         # Esc = 取消
    expect(dialog).to_have_count(0)
    assert len(import_requests(guard)) == 1
    page.locator("#import-preview").click()
    expect(dialog).to_be_visible()
    dialog.locator('[data-role="confirm"]').click()
    expect(page.locator('#import-result [data-role="no-backup"]')).to_have_text("merge 不建立備份。")
    expect(page.locator("#import-result")).to_contain_text("已匯入(merge)")
    assert api(gw, "GET", "/api/deployments/api-a").json()["params"]["api_base"] == "http://127.0.0.1:11/v1"


def test_import_change_invalidates_preview(new_page: NewPage, gw: LiveGateway) -> None:
    seed_member(gw, "api-a")
    first = changed_config(gw, "api-a", "http://127.0.0.1:12/v1")
    second = changed_config(gw, "api-a", "http://127.0.0.1:13/v1")
    page = new_page()
    open_settings(page, gw)
    bodies = record_import_bodies(page)
    choose_import(page, first, mode="merge")

    # 預覽進行中改 mode → 回應到達後不開啟對話框
    held: list[Route] = []

    def hold_first(route: Route) -> None:
        if held:
            route.continue_()
        else:
            held.append(route)

    page.route("**/api/config/import**", hold_first)
    page.locator("#import-preview").click()
    page.wait_for_function("() => true")
    expect(page.locator("#import-preview")).to_be_disabled()
    assert len(held) == 1
    page.locator('input[name="import_mode"][value="replace"]').check()
    with page.expect_response(lambda r: "/api/config/import" in r.url):
        held[0].continue_()
    expect(page.locator("#import-preview")).to_be_enabled()
    expect(import_dialog(page)).to_have_count(0)

    # 預覽 → 取消 → 改檔案 → 預覽:送的是新檔案與新 mode
    page.locator("#import-preview").click()
    expect(import_dialog(page)).to_be_visible()
    import_dialog(page).locator('[data-role="cancel"]').click()
    choose_import(page, second, mode="merge")
    page.locator("#import-preview").click()
    expect(import_dialog(page)).to_be_visible()
    import_dialog(page).locator('[data-role="confirm"]').click()
    expect(page.locator("#import-result")).to_be_visible()
    assert [query(u)["mode"] for u, _ in bodies] == [["merge"], ["replace"], ["merge"], ["merge"]]
    assert [b for _, b in bodies] == [first, first, second, second]
    assert api(gw, "GET", "/api/deployments/api-a").json()["params"]["api_base"] == "http://127.0.0.1:13/v1"


@pytest.mark.expect_http_errors(422)
def test_import_422_lists_all_errors(new_page: NewPage, gw: LiveGateway) -> None:
    bad = {"version": 1, "deployments": [{"id": f"bad {i}", "kind": "nope"} for i in range(30)]}
    data = yaml.safe_dump(bad).encode("utf-8")
    with gw.http() as c:
        ref = c.post("/api/config/import?mode=merge&dry_run=true", content=data,
                     headers={"Content-Type": "application/yaml"})
    assert ref.status_code == 422
    expected = ref.json()["error"]["message"].split("; ")
    assert len(expected) == 20
    page = new_page()
    open_settings(page, gw)
    snap = db_snapshot(gw)
    choose_import(page, data)
    page.locator("#import-preview").click()
    items = page.locator('#import-errors [data-role="errors"] li')
    expect(items).to_have_text(expected)
    expect(import_dialog(page)).to_have_count(0)
    assert len(import_requests(guard_of(page))) == 1
    assert db_snapshot(gw) == snap


@pytest.mark.expect_http_errors(409)
def test_import_409_shown_without_dialog(new_page: NewPage, gw: LiveGateway) -> None:
    seed_member(gw, "c1", kind="cli", provider="claude-cli", model="sonnet", params={}, credential_ref=None,
                capabilities={"ctx": 1000, "tools": True, "vision": False, "efforts": []})
    doc = yaml.safe_load(export_yaml(gw))
    dup = dict(doc["deployments"][0])
    dup["id"] = "c2"
    data = yaml.safe_dump({"version": doc["version"], "deployments": [dup]}, allow_unicode=True).encode("utf-8")
    page = new_page()
    open_settings(page, gw)
    choose_import(page, data, mode="merge")
    with page.expect_response(lambda r: "/api/config/import" in r.url) as rr:
        page.locator("#import-preview").click()
    assert rr.value.status == 409
    expect(page.locator('#import-errors [data-role="errors"] li')).to_have_text([rr.value.json()["error"]["message"]])
    expect(import_dialog(page)).to_have_count(0)


def test_import_too_large_not_sent(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_settings(page, gw)
    choose_import(page, b"#" * (1_048_576 + 1))
    expect(page.locator('#import-errors [data-role="errors"] li')).to_have_text(
        ["設定檔超過上限 1 MiB(1,048,576 bytes),未送出"])
    expect(page.locator("#import-preview")).to_be_disabled()
    page.locator('input[name="import_mode"][value="replace"]').check()
    expect(page.locator("#import-preview")).to_be_disabled()
    assert import_requests(guard_of(page)) == []
    # 剛好 1 MiB 可以預覽(伺服端以 YAML 驗證)
    choose_import(page, b"#" * 1_048_576 + b"", mode="merge")
    expect(page.locator("#import-preview")).to_be_enabled()
    assert import_requests(guard_of(page)) == []


# ---- ⑤ token -----------------------------------------------------------------------------------------

def token_dialog(page: Page) -> Locator:
    return page.locator("#token-dialog")


def confirm_dialog(page: Page) -> Locator:
    return page.locator("dialog").filter(has=page.locator("#confirm-dialog-title"))


def test_token_first_generate_copy_and_wipe(new_page: NewPage, make_ui_gateway: MakeGw) -> None:
    gw = make_ui_gateway(token=False)
    page = new_page(permissions=CLIP)
    open_settings(page, gw, "#token")
    guard = guard_of(page)
    expect(page.locator("#token-rotate")).to_have_text("重新產生 token")

    # 首次:確認文字為「產生存取 token」;預設焦點在取消,Esc 取消不送出
    page.locator("#token-rotate").click()
    confirm = confirm_dialog(page)
    expect(confirm.locator(".dialog-title")).to_have_text("產生存取 token")
    expect(confirm).not_to_contain_text("舊 token 立即失效")
    expect(confirm.locator('[data-role="cancel"]')).to_be_focused()
    page.keyboard.press("Escape")
    expect(confirm).to_have_count(0)
    assert [u for m, u in guard.requests if m == "POST"] == []
    assert token_hash(gw) is None

    page.locator("#token-rotate").click()
    with page.expect_response(lambda r: r.url.endswith("/api/settings/token/rotate")) as rr:
        confirm.locator('[data-role="confirm"]').click()
    token = rr.value.json()["token"]
    dlg = token_dialog(page)
    expect(dlg).to_be_visible()
    expect(dlg.locator("#token-value")).to_have_value(token)
    assert sha(token) == token_hash(gw)
    expect(dlg.locator("#token-export")).to_have_text(f"export SPILLWAY_TOKEN={token}")
    expect(dlg).to_contain_text("關閉後無法再次查看")
    expect(dlg).to_contain_text("剪貼簿可能被同步或留存")
    expect(dlg).not_to_contain_text("舊 token 立即失效")      # 首次沒有舊 token
    dlg.locator('[data-role="copy"]').click()
    expect(page.locator("#msgbar")).to_contain_text("已複製")
    assert page.evaluate("navigator.clipboard.readText()") == token
    dlg.locator('[data-role="close"]').click()                # 已複製 → 直接關閉
    expect(dlg).to_have_count(0)
    expect(confirm_dialog(page)).to_have_count(0)
    assert page_leaks(page, token) == []
    expect(page.locator("#token-status")).to_have_text("已產生")
    expect(page.locator("#ov-token")).to_have_text("已產生")
    # 返回與前進(bfcache)後也不含
    page.goto(gw.url("/ui/ledger.html"))
    page.go_back()
    page.wait_for_selector('html[data-shell="ready"]', state="attached")
    expect(page).to_have_url(gw.url("/ui/settings.html#token"))
    assert page_leaks(page, token) == []
    page.go_forward()
    page.go_back()
    page.wait_for_selector('html[data-shell="ready"]', state="attached")
    assert page_leaks(page, token) == []
    page.reload()
    page.wait_for_selector('#overview[data-loaded="true"]', state="attached")
    expect(page.locator("#token-status")).to_have_text("已產生")
    assert page_leaks(page, token) == []


def test_token_rotate_reprompt_when_not_copied(new_page: NewPage, gw: LiveGateway) -> None:
    old_hash = token_hash(gw)
    page = new_page()                                       # 未授與剪貼簿權限 → 複製失敗
    open_settings(page, gw)
    page.locator("#token-rotate").click()
    confirm = confirm_dialog(page)
    expect(confirm.locator(".dialog-title")).to_have_text("重新產生 token")
    expect(confirm.locator(".dialog-body")).to_have_text("舊 token 立即失效,使用它的 MCP 設定都需更新。")
    expect(confirm.locator('[data-role="cancel"]')).to_be_focused()
    with page.expect_response(lambda r: r.url.endswith("/api/settings/token/rotate")) as rr:
        confirm.locator('[data-role="confirm"]').click()
    token = rr.value.json()["token"]
    assert sha(token) == token_hash(gw) != old_hash
    dlg = token_dialog(page)
    expect(dlg.locator("#token-value")).to_have_value(token)
    expect(dlg).to_contain_text("舊 token 已立即失效")

    # 複製失敗 → 選取文字並提示
    dlg.locator('[data-role="copy"]').click()
    expect(dlg.locator('[data-role="copy-hint"]')).to_be_visible()
    assert page.evaluate("() => { const el = document.getElementById('token-value');"
                         " return el.value.slice(el.selectionStart, el.selectionEnd); }") == token

    # 沒複製就關閉 → 再提示一次;「返回」保留對話框
    dlg.locator('[data-role="close"]').click()
    again = confirm_dialog(page)
    expect(again.locator(".dialog-title")).to_have_text("尚未複製 token")
    expect(again.locator('[data-role="cancel"]')).to_be_focused()
    again.locator('[data-role="cancel"]').click()
    expect(again).to_have_count(0)
    expect(dlg.locator("#token-value")).to_have_value(token)
    # Esc 也走同一個提示
    dlg.locator("#token-value").focus()
    page.keyboard.press("Escape")
    expect(again.locator(".dialog-title")).to_have_text("尚未複製 token")
    again.locator('[data-role="confirm"]').click()
    expect(dlg).to_have_count(0)
    assert page_leaks(page, token) == []
    assert page.evaluate("() => String(window.getSelection())") == ""


@pytest.mark.expect_http_errors(500)
def test_token_unknown_state_uses_rotate_wording(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    page.route("**/api/settings", lambda route: route.fulfill(
        status=500, content_type="application/json",
        body='{"error": {"code": "internal_error", "message": "boom"}}'))
    goto(page, gw, "/ui/settings.html")
    page.wait_for_selector('#overview[data-loaded="true"]', state="attached")
    expect(page.locator("#msgbar")).to_contain_text("boom")
    page.locator("#token-rotate").click()
    confirm = confirm_dialog(page)
    expect(confirm.locator(".dialog-title")).to_have_text("重新產生 token")
    expect(confirm.locator(".dialog-body")).to_contain_text("舊 token 立即失效")
    expect(confirm.locator('[data-role="confirm"]')).to_have_class("btn danger")
    confirm.locator('[data-role="cancel"]').click()
    expect(confirm).to_have_count(0)


def test_token_wiped_on_pagehide_and_back(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    open_settings(page, gw)
    page.locator("#token-rotate").click()
    with page.expect_response(lambda r: r.url.endswith("/api/settings/token/rotate")) as rr:
        confirm_dialog(page).locator('[data-role="confirm"]').click()
    token = rr.value.json()["token"]
    expect(token_dialog(page).locator("#token-value")).to_have_value(token)
    # pagehide(bfcache 凍結前最後一個事件)立即清除:以合成事件直接驗證處理器(headless Chromium 的返回
    # 不一定經 bfcache 還原)
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}))")
    expect(token_dialog(page)).to_have_count(0)
    assert page_leaks(page, token) == []

    page.locator("#token-rotate").click()
    with page.expect_response(lambda r: r.url.endswith("/api/settings/token/rotate")) as rr:
        confirm_dialog(page).locator('[data-role="confirm"]').click()
    token = rr.value.json()["token"]
    expect(token_dialog(page).locator("#token-value")).to_have_value(token)
    # 對話框開著就離開頁面:pagehide 清除;返回(可能由 bfcache 還原)與前進後都不含
    page.goto(gw.url("/ui/ledger.html"))
    page.go_back()
    page.wait_for_selector('html[data-shell="ready"]', state="attached")
    expect(page).to_have_url(gw.url("/ui/settings.html"))
    assert page_leaks(page, token) == []
    expect(token_dialog(page)).to_have_count(0)
    page.go_forward()
    page.go_back()
    page.wait_for_selector('html[data-shell="ready"]', state="attached")
    assert page_leaks(page, token) == []
    assert page.evaluate("() => [window.localStorage.length, window.sessionStorage.length]") == [0, 0]

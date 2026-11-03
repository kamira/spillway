"""畫面骨架與測試框架(CHG-20261103-01 T3;決策 3、5–9、20、22)。

L1(在頁面中驗證共用模組,不依賴 Node):`api.js` 的 `ApiError`(9.1.2 body、非 JSON、fetch 失敗 →
`network_error`)與 `cache: "no-store"`;`dom.js` 的 `h()`(字串子節點為 text node、執行期拒絕事件屬性、
style、srcdoc、formaction、不安全網址與危險 tag);注入 `<img onerror>`、`<script>` 字串不產生元素。
L2:10 頁無 console error / pageerror / CSP 違規、只有本機請求;導覽 8 個連結逐一點擊到達;頁面內以
`api.js` 送 POST、PUT、DELETE 都 2xx(瀏覽器帶的 Origin 通過 HostOrigin);`?x=1#y` 開啟 10 頁沒有非 GET
請求;`confirmDialog` 預設焦點在「取消」、Esc 等於取消;`expect_http_errors(404)` 只略過同源 `/api/` 的 404;
Trusted Types 生效時 locator、`evaluate` 讀取、`drag_to`、下載可用;Chromium 為已快取的 1248;同一個 pytest
行程內 tests/ui 之後的 async 測試照常通過。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Browser, Page, expect

from tests.helpers import TEST_KEY, TEST_KEY_ENV
from tests.helpers.live_gateway import LiveGateway
from tests.helpers.ui import NAV, PAGES, close_and_collect, goto, guard_of, import_module, open_context

REPO = Path(__file__).resolve().parents[2]

NewPage = Callable[..., Page]
MakeGw = Callable[..., LiveGateway]


@pytest.fixture
def gw(make_ui_gateway: MakeGw) -> LiveGateway:
    return make_ui_gateway()


@pytest.fixture
def page(new_page: NewPage, gw: LiveGateway) -> Page:
    p = new_page()
    goto(p, gw, "/ui/")
    return p


# ---- 環境 ---------------------------------------------------------------------------------------

def test_playwright_uses_cached_chromium_1248() -> None:
    proc = subprocess.run([sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    locations = [line.split(":", 1)[1].strip() for line in proc.stdout.splitlines()
                 if line.strip().startswith("Install location:")]
    chromium = [loc for loc in locations if Path(loc).name.startswith(("chromium-", "chromium_headless_shell-"))]
    assert chromium, proc.stdout
    for loc in chromium:
        assert Path(loc).name.endswith("-1248"), f"Playwright 期望的瀏覽器不是 1248:{loc}(決策 3:停下詢問,不下載)"
        assert Path(loc).is_dir(), f"快取的瀏覽器不存在:{loc}(決策 3:不下載)"


def test_fake_fixtures_available(fake_api, fake_cli) -> None:  # noqa: ANN001
    assert os.environ[TEST_KEY_ENV] == TEST_KEY
    assert fake_api.base_url.startswith("http://127.0.0.1:")
    claude = shutil.which("claude")
    assert claude is not None and Path(claude).parent == fake_cli.bin_dir


# ---- 10 頁的外框 ---------------------------------------------------------------------------------

def test_pages_load_clean(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    for spec in PAGES:
        guard = guard_of(page)
        guard.requests.clear()
        goto(page, gw, spec.path, wait_until="networkidle")
        expect(page.locator("main h1")).to_have_text(spec.title)
        expect(page).to_have_title(f"{spec.title} · Spillway")
        links = page.locator("nav.nav a")
        expect(links).to_have_count(len(NAV))
        assert [(links.nth(i).inner_text(), links.nth(i).get_attribute("href")) for i in range(len(NAV))] == NAV
        expect(page.locator('nav.nav a[aria-current="page"]')).to_have_text(spec.current)
        expect(page.locator("#nav-host")).to_have_text(f"127.0.0.1:{gw.port}")
        urls = [u for _, u in guard.requests]
        assert urls and all(u.startswith(gw.base_url + "/") for u in urls), urls
        assert gw.url(f"/ui/assets/js/{spec.script}") in urls
        guard.assert_clean()


def test_nav_links_reach_pages(page: Page, gw: LiveGateway) -> None:
    titles = {p.path: p.title for p in PAGES}
    for text, href in NAV:
        page.locator("nav.nav").get_by_role("link", name=text, exact=True).click()
        expect(page).to_have_url(gw.url(href))
        expect(page.locator("main h1")).to_have_text(titles[href])
        expect(page.locator('nav.nav a[aria-current="page"]')).to_have_text(text)


def test_query_and_hash_send_only_get(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    for spec in PAGES:
        guard_of(page).requests.clear()
        page.goto(gw.url(spec.path) + "?x=1#y", wait_until="networkidle")
        page.wait_for_selector('html[data-shell="ready"]', state="attached")
        methods = [m for m, _ in guard_of(page).requests]
        assert methods and set(methods) == {"GET"}, (spec.file, guard_of(page).requests)


def test_root_redirects_to_ui(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    page.goto(gw.url("/"))
    expect(page).to_have_url(gw.url("/ui/"))
    expect(page.locator("main h1")).to_have_text("總覽")


# ---- api.js ---------------------------------------------------------------------------------------

def test_api_js_mutations_pass_host_origin(page: Page, gw: LiveGateway) -> None:
    import_module(page, "api.js")
    origins: list[tuple[str, str | None]] = []
    page.on("request", lambda r: origins.append((r.method, r.headers.get("origin")))
            if "/api/groups" in r.url else None)
    result = page.evaluate("""async () => {
        const { sendJSON, getJSON } = window.__mods["api.js"];
        const created = await sendJSON("POST", "/api/groups", {name: "t3probe", selector: {any: ["x"]}});
        const updated = await sendJSON("PUT", "/api/groups/t3probe", {selector: {any: ["y"]}});
        const deleted = await sendJSON("DELETE", "/api/groups/t3probe");
        const after = await getJSON("/api/groups");
        return {created: created.name, updated: updated.selector.any, deleted,
                remaining: after.groups.map((g) => g.name)};
    }""")
    assert result == {"created": "t3probe", "updated": ["y"], "deleted": None, "remaining": []}
    sent = {m: o for m, o in origins if m != "GET"}
    assert sent == {"POST": gw.base_url, "PUT": gw.base_url, "DELETE": gw.base_url}


@pytest.mark.expect_http_errors(404, 500, 502)
def test_api_js_errors(page: Page, gw: LiveGateway) -> None:
    import_module(page, "api.js")
    page.route("**/api/__t3/nonjson", lambda r: r.fulfill(status=502, body="bad gateway",
                                                          content_type="text/plain"))
    page.route("**/api/__t3/otherjson", lambda r: r.fulfill(status=500, body='{"detail": "x"}',
                                                            content_type="application/json"))
    page.route("**/api/__t3/down", lambda r: r.abort("connectionrefused"))
    result = page.evaluate("""async () => {
        const { getJSON, sendJSON, ApiError } = window.__mods["api.js"];
        const caught = async (fn) => {
            try { await fn(); return null; }
            catch (e) { return {isApiError: e instanceof ApiError, status: e.status, code: e.code,
                                message: e.message}; }
        };
        return {
            spec: await caught(() => getJSON("/api/nope")),
            post404: await caught(() => sendJSON("POST", "/api/nope", {a: 1})),
            nonjson: await caught(() => getJSON("/api/__t3/nonjson")),
            otherjson: await caught(() => getJSON("/api/__t3/otherjson")),
            down: await caught(() => getJSON("/api/__t3/down")),
        };
    }""")
    assert result["spec"]["isApiError"] and result["spec"]["status"] == 404
    assert result["spec"]["code"] == "not_found"
    assert result["post404"]["code"] in ("not_found", "method_not_allowed")
    assert result["nonjson"] == {"isApiError": True, "status": 502, "code": "http_error",
                                 "message": "伺服器回應錯誤(HTTP 502)"}
    assert result["otherjson"]["status"] == 500 and result["otherjson"]["code"] == "http_error"
    assert result["down"] == {"isApiError": True, "status": 0, "code": "network_error",
                              "message": "無法連線到 spillway"}
    # fetch 被中止時 Chromium 會記一筆 net::ERR_*;這是本案刻意製造的,明確取走
    guard_of(page).take(lambda v: "net::ERR_" in v.text)


def test_h_value_stringified_once(page: Page) -> None:
    import_module(page, "dom.js")
    got = page.evaluate("""() => {
        const v = {n: 0, toString() { return this.n++ ? "javascript:alert(1)" : "/ui/ok.html"; }};
        return window.__mods["dom.js"].h("a", {href: v}).getAttribute("href");
    }""")
    assert got == "/ui/ok.html"


def test_h_allows_safe_multi_url_attrs(page: Page) -> None:
    import_module(page, "dom.js")
    got = page.evaluate("""() => {
        const { h } = window.__mods["dom.js"];
        return [h("img", {srcset: "/ui/a.png 1x, ./b.png 2x"}).getAttribute("srcset"),
                h("a", {href: "#x", ping: "/api/a /api/b"}).getAttribute("ping")];
    }""")
    assert got == ["/ui/a.png 1x, ./b.png 2x", "/api/a /api/b"]


def test_api_js_bad_2xx_body(page: Page) -> None:
    import_module(page, "api.js")
    page.route("**/api/__t3/okbad", lambda r: r.fulfill(status=200, body="not json", content_type="text/html"))
    page.route("**/api/__t3/okbadjson", lambda r: r.fulfill(status=200, body="{oops",
                                                           content_type="application/json"))
    result = page.evaluate("""async () => {
        const { getJSON, sendJSON, ApiError } = window.__mods["api.js"];
        const caught = async (fn) => {
            try { await fn(); return null; }
            catch (e) { return {isApiError: e instanceof ApiError, status: e.status, code: e.code, message: e.message}; }
        };
        return [await caught(() => getJSON("/api/__t3/okbad")),
                await caught(() => sendJSON("POST", "/api/__t3/okbadjson", {}))];
    }""")
    expected = {"isApiError": True, "status": 200, "code": "bad_response", "message": "伺服器回應的內容無法解讀"}
    assert result == [expected, expected]


@pytest.mark.expect_http_errors(422)
def test_api_js_uses_no_store(page: Page) -> None:
    import_module(page, "api.js")
    inits = page.evaluate("""async () => {
        const seen = [];
        const real = window.fetch;
        window.fetch = (input, init) => { seen.push({cache: init && init.cache, method: init && init.method});
                                          return real(input, init); };
        try {
            const { getJSON, sendJSON, sendText } = window.__mods["api.js"];
            await getJSON("/api/settings");
            await sendJSON("PUT", "/api/settings/context", await getJSON("/api/settings/context"));
            await sendText("POST", "/api/config/import?dry_run=true", "deployments: []\\n",
                           "application/yaml").catch(() => null);
        } finally { window.fetch = real; }
        return seen;
    }""")
    assert len(inits) >= 4
    assert {i["cache"] for i in inits} == {"no-store"}


# ---- dom.js ---------------------------------------------------------------------------------------

def test_h_string_children_are_text(page: Page) -> None:
    import_module(page, "dom.js")
    out = page.evaluate("""() => {
        const { h } = window.__mods["dom.js"];
        const p = h("p", {}, "<b>x</b>");
        const mixed = h("div", {"data-id": "<x>", class: "card"}, "a", 1, null, false, [h("span", {}, "b")]);
        return {text: p.textContent, elems: p.childElementCount, nodes: p.childNodes.length,
                nodeType: p.firstChild.nodeType, mixedText: mixed.textContent,
                mixedElems: mixed.childElementCount, dataId: mixed.dataset.id};
    }""")
    assert out == {"text": "<b>x</b>", "elems": 0, "nodes": 1, "nodeType": 3, "mixedText": "a1b",
                   "mixedElems": 1, "dataId": "<x>"}


def test_h_injection_strings_create_no_elements(page: Page) -> None:
    import_module(page, "dom.js")
    out = page.evaluate("""() => {
        const { h } = window.__mods["dom.js"];
        const before = document.querySelectorAll("img, script").length;
        const box = h("div", {id: "inj", title: "<img src=x onerror=alert(1)>"},
                      "<img src=x onerror=alert(1)>", "<script>alert(2)</scr" + "ipt>");
        document.querySelector("main").append(box);
        return {added: document.querySelectorAll("img, script").length - before, text: box.textContent};
    }""")
    assert out["added"] == 0
    assert out["text"] == "<img src=x onerror=alert(1)><script>alert(2)</script>"
    expect(page.locator("#inj")).to_have_text("<img src=x onerror=alert(1)><script>alert(2)</script>")
    assert guard_of(page).dialogs == []


@pytest.mark.parametrize("call", [
    'h("button", {onclick: "alert(1)"})', 'h("button", {ONCLICK: "x"})', 'h("div", {onMouseOver: "x"})',
    'h("div", {style: "color:red"})', 'h("div", {srcdoc: "<p>"})', 'h("button", {formaction: "/api/x"})',
    'h("a", {href: "javascript:alert(1)"})', 'h("a", {href: "//evil.example/"})',
    'h("a", {href: "https://evil.example/"})', 'h("img", {src: "http://evil.example/x.png"})',
    'h("a", {href: "data:text/html,x"})', 'h("form", {action: "https://evil.example"})',
    'h("img", {srcset: "/ui/a.png 1x, https://evil.example/b.png 2x"})', 'h("img", {srcset: ""})',
    'h("video", {poster: "https://evil.example/p.png"})',
    'h("a", {href: "/ui/", ping: "/api/x https://evil.example/track"})',
    'h("script")', 'h("SCRIPT")', 'h("iframe")', 'h("base", {href: "/ui/"})', 'h("style")',
    'h("object")', 'h("embed")', 'h("link")', 'h("meta")', 'h("img src=x onerror=alert(1)")',
])
def test_h_rejects(page: Page, call: str) -> None:
    import_module(page, "dom.js")
    out = page.evaluate(f"""() => {{
        const {{ h, DomError }} = window.__mods["dom.js"];
        try {{ {call}; return "no-throw"; }} catch (e) {{ return e instanceof DomError ? "DomError" : e.name; }}
    }}""")
    assert out == "DomError"


@pytest.mark.parametrize("url", ["./x.html", "/ui/members.html#a", "/api/ledger/export.csv?x=1", "#top",
                                 "?q=1", "blob:http://127.0.0.1:1/abc"])
def test_h_allows_safe_urls(page: Page, url: str) -> None:
    import_module(page, "dom.js")
    got = page.evaluate("""(u) => window.__mods["dom.js"].h("a", {href: u}).getAttribute("href")""", url)
    assert got == url


# ---- ui.js ---------------------------------------------------------------------------------------

def test_confirm_dialog_focus_escape_and_confirm(page: Page) -> None:
    import_module(page, "ui.js")
    page.evaluate("""() => {
        window.__r1 = window.__mods["ui.js"].confirmDialog({title: "刪除成員", body: "會一併刪除 tags",
                                                            confirmLabel: "刪除", danger: true});
    }""")
    dialog = page.locator("dialog.dialog")
    expect(dialog).to_be_visible()
    expect(dialog).to_have_class("dialog dialog-danger")
    expect(dialog.get_by_role("heading")).to_have_text("刪除成員")
    expect(dialog.get_by_role("button", name="取消")).to_be_focused()
    expect(dialog.get_by_role("button", name="刪除")).to_have_class("btn danger")
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    assert page.evaluate("() => window.__r1") is False

    page.evaluate("""() => { window.__r2 = window.__mods["ui.js"].confirmDialog({title: "確認"}); }""")
    expect(page.get_by_role("button", name="取消")).to_be_focused()
    page.get_by_role("button", name="確定").click()
    expect(page.locator("dialog")).to_have_count(0)
    assert page.evaluate("() => window.__r2") is True

    page.evaluate("""() => { window.__r3 = window.__mods["ui.js"].confirmDialog({title: "確認"}); }""")
    page.get_by_role("button", name="取消").click()
    assert page.evaluate("() => window.__r3") is False


def test_message_bar_and_errors(page: Page) -> None:
    import_module(page, "ui.js")
    import_module(page, "api.js")
    page.evaluate("""() => {
        const { ApiError } = window.__mods["api.js"];
        window.__mods["ui.js"].showError(new ApiError(0, "network_error", "x"));
    }""")
    bar = page.locator("#msgbar")
    expect(bar).to_be_visible()
    expect(bar).to_have_attribute("role", "alert")
    expect(bar.locator(".msg-text")).to_have_text("無法連線到 spillway")
    page.evaluate("""() => {
        const { ApiError } = window.__mods["api.js"];
        window.__mods["ui.js"].showError(new ApiError(422, "validation_error", "<b>body.name</b>: 不合法"));
    }""")
    expect(bar.locator(".msg-text")).to_have_text("<b>body.name</b>: 不合法")
    assert bar.locator("b").count() == 0
    bar.get_by_role("button", name="關閉").click()
    expect(bar).to_be_hidden()


def test_formatters_and_labels(page: Page) -> None:
    import_module(page, "ui.js")
    import_module(page, "strings.js")
    out = page.evaluate("""() => {
        const ui = window.__mods["ui.js"]; const s = window.__mods["strings.js"];
        const badge = ui.kindBadge("cli");
        const unknown = ui.codeLabel(s.STATUS, "weird");
        return {local: ui.fmtLocal("2026-11-02T16:30:00Z"), utc: ui.fmtUTC("2026-11-02T16:30:00Z"),
                usd: ui.fmtUSD(6), usd2: ui.fmtUSD(0.125), badge: badge.textContent, badgeTitle: badge.title,
                badgeClass: badge.className, unknown: unknown.textContent,
                cooling: s.label(s.COOLING_REASON, "rate_limited"), win: s.label(s.WINDOW, "monthly"),
                budget: s.label(s.BUDGET_TYPE, "credit_pool"), nav: s.NAV.map((n) => n[0])};
    }""")
    assert out["local"] == "2026/11/03 00:30:00"           # Asia/Taipei
    assert out["utc"] == "2026-11-02T16:30:00Z (UTC)"
    assert out["usd"] == "$6.00" and out["usd2"] == "$0.13"
    assert (out["badge"], out["badgeTitle"], out["badgeClass"]) == ("訂閱 CLI", "cli", "badge b-cli")
    assert out["unknown"] == "weird"
    assert (out["cooling"], out["win"], out["budget"]) == ("限流", "每月", "點數池(USD)")
    assert out["nav"] == [t for t, _ in NAV]


def test_navigate_helper(page: Page, gw: LiveGateway) -> None:
    import_module(page, "ui.js")
    bad = page.evaluate("""() => { try { window.__mods["ui.js"].navigate("javascript:alert(1)"); return "ok"; }
                                   catch (e) { return e.name; } }""")
    assert bad == "DomError"
    page.evaluate("""() => window.__mods["ui.js"].navigate("/ui/members.html#m1")""")
    expect(page).to_have_url(gw.url("/ui/members.html#m1"))
    expect(page.locator("main h1")).to_have_text("成員")


# ---- console 守門 --------------------------------------------------------------------------------

def _is_error(msg) -> bool:  # noqa: ANN001
    return msg.type == "error"


@pytest.mark.expect_http_errors(404)
def test_expect_http_errors_only_skips_same_origin_api(page: Page) -> None:
    guard = guard_of(page)
    with page.expect_console_message(_is_error) as first:
        page.evaluate("""async () => { await fetch("/api/nope", {cache: "no-store"}); }""")
    assert "404" in first.value.text
    assert guard.violations() == []          # 同源 /api/ 的 404 已宣告 → 略過
    # 同源但不是 /api/ 的 404 仍判違規
    with page.expect_console_message(_is_error):
        page.evaluate("""async () => { await fetch("/ui/nope.html", {cache: "no-store"}); }""")
    assert [v.kind for v in guard.violations()] == ["console"]
    assert "/ui/nope.html" in guard.violations()[0].url
    # 未宣告的狀態碼(409)仍判違規
    page.route("**/api/__t3/conflict", lambda r: r.fulfill(status=409, body="{}", content_type="application/json"))
    with page.expect_console_message(_is_error):
        page.evaluate("""async () => { await fetch("/api/__t3/conflict", {cache: "no-store"}); }""")
    assert len(guard.violations()) == 2
    # 其他 console error 與 pageerror 也判違規
    with page.expect_console_message(_is_error):
        page.evaluate("""() => console.error("t3 probe error")""")
    with page.expect_event("pageerror"):
        page.evaluate("""() => { setTimeout(() => { throw new Error("t3 boom"); }, 0); }""")
    kinds = [v.kind for v in guard.violations()]
    assert kinds == ["console", "console", "console", "pageerror"]
    assert len(guard.take(lambda v: True)) == 4


def test_csp_violation_is_reported(page: Page) -> None:
    guard = guard_of(page)
    with page.expect_console_message(lambda m: m.type == "error" and "Content Security Policy" in m.text):
        page.evaluate("""() => { const i = document.createElement("img");
                                 i.setAttribute("src", "data:image/gif;base64,R0lGODlhAQABAAAAACw=");
                                 document.body.append(i); }""")
    assert guard.take(lambda v: "Content Security Policy" in v.text)


def test_non_local_request_blocked_and_recorded(page: Page) -> None:
    guard = guard_of(page)
    # fetch 會先被 CSP connect-src 擋下(不會成為請求);導覽不受 connect-src 限制,改由 context.route 中止
    with pytest.raises(PlaywrightError):
        page.goto("http://example.invalid/x")
    taken = guard.take(lambda v: v.kind == "request" or (v.kind == "console" and "net::ERR" in v.text))
    guard.assert_clean()
    assert any(v.kind == "request" and v.url.startswith("http://example.invalid/") for v in taken)



def test_close_and_collect_catches_late_events(browser: Browser, gw: LiveGateway) -> None:
    """T3 審議 M2:最後一個 Playwright 呼叫之後才發生的 console error 與 pageerror,關閉 context 時才送達。"""
    ctx, guard = open_context(browser)
    page = ctx.new_page()
    goto(page, gw, "/ui/")
    page.evaluate("""() => { setTimeout(() => { console.error("t3 late error");
                                                throw new Error("t3 late boom"); }, 50); }""")
    time.sleep(0.3)   # 刻意不呼叫 Playwright:事件留在佇列裡
    found = close_and_collect([(ctx, guard)])
    assert [(v.kind, v.text) for v in found] == [("console", "t3 late error"), ("pageerror", "t3 late boom")]


def test_manual_dialogs_beforeunload(page: Page, gw: LiveGateway) -> None:
    guard = guard_of(page)
    page.evaluate("""() => { window.addEventListener("beforeunload", (e) => { e.preventDefault(); e.returnValue = ""; }); }""")
    page.locator("main h1").click()   # beforeunload 需要使用者互動才會顯示
    seen: list[str] = []

    def accept(dialog) -> None:  # noqa: ANN001
        seen.append(dialog.type)
        dialog.accept()

    with guard.manual_dialogs():
        page.once("dialog", accept)
        page.goto(gw.url("/ui/members.html"))
    assert seen == ["beforeunload"]
    assert guard.dialogs[-1][0] == "beforeunload"
    expect(page.locator("main h1")).to_have_text("成員")
    # 區塊外恢復預設:記錄後 dismiss
    with page.expect_event("dialog"):
        page.evaluate("""() => { setTimeout(() => alert("t3"), 0); }""")
    assert guard.dialogs[-1] == ("alert", "t3")
    expect(page.locator("main h1")).to_have_text("成員")   # alert 已被 dismiss,頁面可操作


# ---- Trusted Types 下 Playwright 的必要操作 -----------------------------------------------------

def test_trusted_types_enforced_and_playwright_ops_work(page: Page, tmp_path: Path) -> None:
    # TT 生效:字串指派到 HTML sink 被拒
    with page.expect_console_message(lambda m: m.type == "error" and "TrustedHTML" in m.text):
        tt = page.evaluate("""() => { try { document.body.innerHTML = "<b>x</b>"; return "allowed"; }
                                      catch (e) { return e.name + ": " + e.message; } }""")
    assert tt.startswith("TypeError") and "TrustedHTML" in tt
    assert guard_of(page).take(lambda v: "TrustedHTML" in v.text)
    # locator 與 evaluate 讀取
    expect(page.get_by_role("navigation", name="主選單").get_by_role("link")).to_have_count(8)
    assert page.locator("main h1").inner_text() == "總覽"
    assert page.evaluate("() => document.querySelectorAll('nav a').length") == 8
    # drag_to(HTML5 DnD)
    page.evaluate("""() => {
        const src = document.createElement("div"); src.id = "dragsrc"; src.draggable = true;
        src.textContent = "拖我"; src.className = "member";
        const dst = document.createElement("div"); dst.id = "dropdst"; dst.className = "drop";
        dst.textContent = "放這裡";
        src.addEventListener("dragstart", (e) => e.dataTransfer.setData("text/plain", "m1"));
        dst.addEventListener("dragover", (e) => e.preventDefault());
        dst.addEventListener("drop", (e) => { e.preventDefault(); dst.dataset.got = e.dataTransfer.getData("text/plain"); });
        document.querySelector("main").append(src, dst);
    }""")
    page.locator("#dragsrc").drag_to(page.locator("#dropdst"))
    expect(page.locator("#dropdst")).to_have_attribute("data-got", "m1")
    # 下載(Blob + <a download>)
    with page.expect_download() as info:
        page.evaluate("""() => {
            const url = URL.createObjectURL(new Blob(["hello 下載"], {type: "text/plain;charset=utf-8"}));
            const a = document.createElement("a"); a.setAttribute("href", url); a.download = "t3.txt";
            document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
        }""")
    download = info.value
    assert download.suggested_filename == "t3.txt"
    target = tmp_path / "t3.txt"
    download.save_as(target)
    assert target.read_text(encoding="utf-8") == "hello 下載"


# ---- 事件迴圈:tests/ui 之後的 async 測試 -------------------------------------------------------

def test_async_tests_after_ui_module_pass() -> None:
    """在子行程依序跑本檔的一個 Playwright 測試與一個 anyio 測試模組,後者必須全過。"""
    env = {**os.environ, "PYTHONPATH": str(REPO / "src"), "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/ui/test_shell.py::test_root_redirects_to_ui", "tests/unit/api/test_drain.py"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    assert " passed" in proc.stdout and "failed" not in proc.stdout

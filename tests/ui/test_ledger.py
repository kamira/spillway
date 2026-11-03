"""UI-6 帳本(CHG-20261103-01 T4 L2;決策 16、20;設計差異 #8)。

篩選每個欄位改變後網址 query 與 `/api/ledger` 請求參數一致(「只看 fallback」= `min_depth=1`);起訖以本地
日期換算 ISO(context 固定 Asia/Taipei:起 = 本地 00:00、訖 = 隔日本地 00:00);分頁;「匯出 CSV」連結的 query
與目前篩選相同,`total > 100000` 時停用並提示;點選列顯示 payload(純文字、`no-store`、保存至、複製);
410「已過保存期限」、404「此次未記錄內容」;孤兒列顯示原 id 與「(已刪除)」;`error` 含 HTML 時以文字顯示。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Page, Request, Response, expect

from spillway.clock import FakeClock
from tests.helpers.live_gateway import LiveGateway
from tests.helpers.ui import MakeGw, NewPage, add_member, add_route, api, goto, guard_of, query_of, wait_loaded
from tests.helpers.ui_ledger import bulk_ledger, insert_ledger, insert_payload


NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)
XSS = "<img src=x onerror=alert(1)><script>alert(2)</script>"


def is_list(r: Request | Response) -> bool:
    return urlsplit(r.url).path == "/api/ledger"


def ids(page: Page) -> list[int]:
    return [int(x) for x in page.locator("#ledger-body tr[data-ledger-id]").evaluate_all(
        "rs => rs.map(r => r.dataset.ledgerId)")]


@pytest.fixture
def gw(make_ui_gateway: MakeGw) -> LiveGateway:
    g = make_ui_gateway(clock=FakeClock(NOW))
    add_member(g, "m1", kind="local")
    add_member(g, "m2", kind="local")
    add_route(g, "rt")
    add_route(g, "other")
    return g


def open_ledger(new_page: NewPage, gw: LiveGateway, path: str = "/ui/ledger.html", **kw) -> Page:  # noqa: ANN003
    page = new_page(**kw)
    goto(page, gw, path)
    wait_loaded(page, "#ledger-table")
    return page


def change(page: Page, action: Callable[[], None]) -> dict[str, str]:
    """執行改變篩選的動作,回傳 /api/ledger 請求的 query,並確認網址 query 與它相同。"""
    with page.expect_request(is_list) as req:
        action()
    wait_loaded(page, "#ledger-table")
    sent = query_of(req.value.url)
    assert query_of(page.url) == sent, (page.url, req.value.url)
    return sent


def test_filters_sync_url_api_and_export(new_page: NewPage, gw: LiveGateway) -> None:
    insert_ledger(gw, 1, NOW - timedelta(hours=1), deployment="m1", route="rt", status="ok")
    insert_ledger(gw, 2, NOW - timedelta(hours=2), deployment="m1", route="rt", status="error", fallback_depth=1)
    insert_ledger(gw, 3, NOW - timedelta(hours=3), deployment="m2", route="other", status="error", fallback_depth=2)
    page = open_ledger(new_page, gw)
    assert ids(page) == [1, 2, 3]
    assert query_of(page.url) == {}
    export = page.locator("#export-csv")
    expect(export).to_have_attribute("href", "/api/ledger/export.csv")

    assert change(page, lambda: page.locator("#f-route").select_option("rt")) == {"route": "rt"}
    assert ids(page) == [1, 2]
    assert change(page, lambda: page.locator("#f-deployment").select_option("m1")) == {
        "route": "rt", "deployment": "m1"}
    assert change(page, lambda: page.locator("#f-status").select_option("error")) == {
        "route": "rt", "deployment": "m1", "status": "error"}
    assert ids(page) == [2]
    expect(page.locator("#f-status option:checked")).to_have_text("錯誤")
    assert change(page, lambda: page.locator("#f-fallback").check()) == {
        "route": "rt", "deployment": "m1", "status": "error", "min_depth": "1"}
    assert ids(page) == [2]
    q = change(page, lambda: page.locator("#f-from").fill("2026-11-03"))
    assert q["from"] == "2026-11-02T16:00:00Z"
    q = change(page, lambda: page.locator("#f-to").fill("2026-11-03"))
    assert q == {"route": "rt", "deployment": "m1", "status": "error", "min_depth": "1",
                 "from": "2026-11-02T16:00:00Z", "to": "2026-11-03T16:00:00Z"}
    href = export.get_attribute("href")
    assert href is not None and urlsplit(href).path == "/api/ledger/export.csv"
    assert query_of(href) == q
    expect(page.locator("#export-hint")).to_be_hidden()

    # 「只看 fallback」取消、狀態改回全部:參數一併移除
    q = change(page, lambda: page.locator("#f-fallback").uncheck())
    assert "min_depth" not in q
    q = change(page, lambda: page.locator("#f-status").select_option(""))
    assert "status" not in q

    # 以目前網址重新開啟:篩選欄位還原,請求參數相同
    with page.expect_request(is_list) as req:
        page.reload()
    wait_loaded(page, "#ledger-table")
    assert query_of(req.value.url) == q
    expect(page.locator("#f-from")).to_have_value("2026-11-03")
    expect(page.locator("#f-to")).to_have_value("2026-11-03")
    expect(page.locator("#f-route")).to_have_value("rt")
    expect(page.locator("#f-deployment")).to_have_value("m1")

    # 匯出連結實際可下載,列數與篩選相同
    with page.expect_download() as dl:
        export.click()
    lines = open(dl.value.path(), encoding="utf-8-sig").read().strip().splitlines()
    assert len(lines) == 1 + len(ids(page))


def test_date_boundaries_asia_taipei(new_page: NewPage, gw: LiveGateway) -> None:
    edge = datetime(2026, 11, 2, 16, 0, 0, tzinfo=timezone.utc)      # = 2026-11-03 00:00 台北
    insert_ledger(gw, 1, edge - timedelta(seconds=1), deployment="m1")                  # 11-02 23:59:59
    insert_ledger(gw, 2, edge, deployment="m1")                                         # 11-03 00:00:00
    insert_ledger(gw, 3, edge + timedelta(days=1) - timedelta(seconds=1), deployment="m1")   # 11-03 23:59:59
    insert_ledger(gw, 4, edge + timedelta(days=1), deployment="m1")                     # 11-04 00:00:00
    page = open_ledger(new_page, gw)
    assert ids(page) == [4, 3, 2, 1]
    change(page, lambda: page.locator("#f-from").fill("2026-11-03"))
    q = change(page, lambda: page.locator("#f-to").fill("2026-11-03"))
    assert q == {"from": "2026-11-02T16:00:00Z", "to": "2026-11-03T16:00:00Z"}
    assert ids(page) == [3, 2]
    # 時間以本地時間顯示,UTC 在 title
    t = page.locator('#ledger-body tr[data-ledger-id="2"] time')
    expect(t).to_have_text("2026/11/03 00:00:00")
    expect(t).to_have_attribute("title", "2026-11-02T16:00:00Z (UTC)")
    # 跨月:訖 = 10-31 → 隔日 11-01 00:00 台北
    q = change(page, lambda: page.locator("#f-to").fill("2026-10-31"))
    assert q["to"] == "2026-10-31T16:00:00Z"
    # 同一資料庫、不同時區的 context:換算跟著瀏覽器的本地時區
    other = open_ledger(new_page, gw, timezone_id="UTC")
    q = change(other, lambda: other.locator("#f-from").fill("2026-11-03"))
    assert q == {"from": "2026-11-03T00:00:00Z"}


def test_paging(new_page: NewPage, gw: LiveGateway) -> None:
    for i in range(1, 121):
        insert_ledger(gw, i, NOW - timedelta(minutes=i), deployment="m1")
    page = open_ledger(new_page, gw)
    expect(page.locator("#page-info")).to_have_text("第 1 / 3 頁,共 120 列")
    assert ids(page) == list(range(1, 51))
    expect(page.locator("#page-prev")).to_be_disabled()
    assert change(page, lambda: page.locator("#page-next").click()) == {"page": "2"}
    assert ids(page) == list(range(51, 101))
    assert change(page, lambda: page.locator("#page-next").click()) == {"page": "3"}
    assert ids(page) == list(range(101, 121))
    expect(page.locator("#page-next")).to_be_disabled()
    expect(page.locator("#page-info")).to_have_text("第 3 / 3 頁,共 120 列")
    # 匯出連結不帶 page
    assert query_of(page.locator("#export-csv").get_attribute("href") or "") == {}
    assert change(page, lambda: page.locator("#page-prev").click()) == {"page": "2"}
    # 改篩選回到第 1 頁
    assert change(page, lambda: page.locator("#f-deployment").select_option("m1")) == {"deployment": "m1"}
    expect(page.locator("#page-info")).to_have_text("第 1 / 3 頁,共 120 列")


def test_export_disabled_over_limit(new_page: NewPage, gw: LiveGateway) -> None:
    bulk_ledger(gw, 100_001, NOW - timedelta(hours=1), deployment="m1", status="ok")
    insert_ledger(gw, 200_000, NOW - timedelta(hours=2), deployment="m2", status="error")
    page = open_ledger(new_page, gw)
    export = page.locator("#export-csv")
    expect(page.locator("#page-info")).to_contain_text("共 100,002 列")
    expect(export).not_to_have_attribute("href", "/api/ledger/export.csv")
    assert export.get_attribute("href") is None
    expect(export).to_have_attribute("aria-disabled", "true")
    hint = page.locator("#export-hint")
    expect(hint).to_be_visible()
    expect(hint).to_have_text("符合的列超過 100,000 列,請縮小範圍後再匯出")
    # 縮小範圍後恢復
    q = change(page, lambda: page.locator("#f-status").select_option("error"))
    assert q == {"status": "error"}
    expect(export).to_have_attribute("href", "/api/ledger/export.csv?status=error")
    expect(export).not_to_have_attribute("aria-disabled", "true")
    expect(hint).to_be_hidden()


def test_payload_panel(new_page: NewPage, gw: LiveGateway) -> None:
    ts = NOW - timedelta(hours=1)
    req_text = '{"messages": [{"role": "user", "content": "<b>hi</b> & <script>alert(3)</script>"}]}'
    resp_text = "回應 <img src=x onerror=alert(4)>"
    insert_ledger(gw, 7, ts, deployment="m1", tokens_in=3120, tokens_out=842, cost_usd=0.0214,
                  latency_ms=18400, slot=1, effort="high")
    insert_payload(gw, 7, ts, req_text, resp_text, truncated=True, redacted=True)
    retention = api(gw, "GET", "/api/settings/context").json()["context_retention_days"]
    page = open_ledger(new_page, gw, permissions=["clipboard-read", "clipboard-write"])
    r = page.locator('#ledger-body tr[data-ledger-id="7"]')
    expect(r.locator("td").nth(6)).to_have_text("3,120 / 842")
    expect(r.locator("td").nth(8)).to_have_text("$0.0214")
    expect(r.locator("td").nth(10)).to_have_text("18.4 s")
    with page.expect_response(lambda x: x.url.endswith("/api/ledger/7/payload")) as resp:
        r.click()
    assert resp.value.status == 200
    assert resp.value.headers["cache-control"] == "no-store"
    panel = page.locator("#payload-panel")
    expect(panel).to_be_visible()
    expect(page.locator('#payload-panel[data-loaded="true"]')).to_have_count(1)
    pre = page.locator("#payload-text")
    expect(pre).to_have_text(req_text)
    expect(panel.locator('[data-role="truncated"]')).to_have_text("已截斷")
    expect(panel.locator('[data-role="redacted"]')).to_have_text("已遮罩")
    kept = ts + timedelta(days=retention)
    kept_el = panel.locator('[data-role="kept-until"]')
    expect(kept_el).to_contain_text("保存至")
    expect(kept_el.locator("time")).to_have_attribute("datetime", kept.strftime("%Y-%m-%dT%H:%M:%SZ"))
    expect(r).to_have_attribute("aria-selected", "true")

    panel.get_by_role("button", name="複製").click()
    expect(page.locator("#msgbar")).to_have_text("已複製到剪貼簿×")
    assert page.evaluate("() => navigator.clipboard.readText()") == req_text

    panel.get_by_role("tab", name="回應").click()
    expect(pre).to_have_text(resp_text)
    panel.get_by_role("button", name="複製").click()
    assert page.evaluate("() => navigator.clipboard.readText()") == resp_text

    assert page.locator("img").count() == 0
    assert page.locator("script").count() == 1
    assert pre.evaluate("el => el.children.length") == 0
    assert guard_of(page).dialogs == []

    panel.get_by_role("button", name="關閉").click()
    expect(panel).to_be_hidden()
    # 關閉後內容不留在 DOM
    expect(page.locator("#payload-text")).to_have_count(0)
    assert page.locator("#payload-body").evaluate("el => el.textContent") == ""
    assert "alert(3)" not in page.content()


@pytest.mark.expect_http_errors(404, 410)
def test_payload_expired_and_not_recorded(new_page: NewPage, gw: LiveGateway) -> None:
    api(gw, "PUT", "/api/settings/context", {"context_retention_days": 7})    # 明設:30 天前的列已過期
    old = NOW - timedelta(days=30)
    insert_ledger(gw, 1, old, deployment="m1")
    insert_payload(gw, 1, old, '{"messages": []}', "x")
    insert_ledger(gw, 2, NOW - timedelta(hours=1), deployment="m1")
    page = open_ledger(new_page, gw)
    panel = page.locator("#payload-panel")
    with page.expect_response(lambda x: x.url.endswith("/api/ledger/1/payload")) as resp:
        page.locator('#ledger-body tr[data-ledger-id="1"]').click()
    assert resp.value.status == 410 and resp.value.headers["cache-control"] == "no-store"
    expect(panel.locator('[data-role="payload-notice"]')).to_have_text("已過保存期限")
    with page.expect_response(lambda x: x.url.endswith("/api/ledger/2/payload")) as resp:
        page.locator('#ledger-body tr[data-ledger-id="2"]').press("Enter")
    assert resp.value.status == 404
    expect(panel.locator('[data-role="payload-notice"]')).to_have_text("此次未記錄內容")
    expect(page.locator("#payload-text")).to_have_count(0)
    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()


def test_orphan_rows_show_original_id(new_page: NewPage, gw: LiveGateway) -> None:
    insert_ledger(gw, 1, NOW - timedelta(hours=1), deployment="m1")
    insert_ledger(gw, 2, NOW - timedelta(hours=2), deployment="ghost")
    add_member(gw, "gone", kind="local")
    insert_ledger(gw, 3, NOW - timedelta(hours=3), deployment="gone")
    api(gw, "DELETE", "/api/deployments/gone")
    page = open_ledger(new_page, gw)
    cell = lambda i: page.locator(f'#ledger-body tr[data-ledger-id="{i}"] td.ledger-member-cell')  # noqa: E731
    expect(cell(1)).to_have_text("m1")
    expect(cell(2)).to_have_text("ghost(已刪除)")
    expect(cell(3)).to_have_text("gone(已刪除)")
    # 已刪除的成員不在成員選單中
    expect(page.locator("#f-deployment option")).to_have_text(["全部", "m1", "m2"])


@pytest.mark.expect_http_errors(404)
def test_error_html_is_text(new_page: NewPage, gw: LiveGateway) -> None:
    insert_ledger(gw, 1, NOW - timedelta(hours=1), deployment=XSS.replace(" ", ""), route=XSS, status="error",
                  error=XSS)
    page = open_ledger(new_page, gw)
    r = page.locator('#ledger-body tr[data-ledger-id="1"]')
    expect(r.locator(".ledger-error")).to_have_text(XSS)
    expect(r.locator("td").nth(1)).to_have_text(XSS)
    expect(r.locator("td.ledger-member-cell")).to_have_text(XSS.replace(" ", "") + "(已刪除)")
    with page.expect_response(lambda x: x.url.endswith("/api/ledger/1/payload")):
        r.click()
    expect(page.locator("#payload-panel .ledger-error")).to_have_text(XSS)
    assert page.locator("img").count() == 0
    assert page.locator("script").count() == 1
    assert guard_of(page).dialogs == []


def test_double_click_next_moves_one_page(new_page: NewPage, gw: LiveGateway) -> None:
    for i in range(1, 121):
        insert_ledger(gw, i, NOW - timedelta(minutes=i), deployment="m1")
    page = open_ledger(new_page, gw)
    sent: list[dict[str, str]] = []
    page.on("request", lambda r: sent.append(query_of(r.url)) if is_list(r) else None)
    page.locator("#page-next").dblclick()
    expect(page.locator("#page-info")).to_have_text("第 2 / 3 頁,共 120 列")
    wait_loaded(page, "#ledger-table")
    assert ids(page) == list(range(51, 101))
    assert query_of(page.url) == {"page": "2"}
    assert sent and all(q == {"page": "2"} for q in sent), sent
    # 再按一次下一頁:由最後一次回應的頁碼 +1
    assert change(page, lambda: page.locator("#page-next").click()) == {"page": "3"}


def test_page_out_of_range_clamps_to_last(new_page: NewPage, gw: LiveGateway) -> None:
    for i in range(1, 121):
        insert_ledger(gw, i, NOW - timedelta(minutes=i), deployment="m1")
    page = new_page()
    pages: list[str | None] = []
    page.on("request", lambda r: pages.append(query_of(r.url).get("page")) if is_list(r) else None)
    goto(page, gw, "/ui/ledger.html?page=9")
    expect(page.locator("#page-info")).to_have_text("第 3 / 3 頁,共 120 列")
    wait_loaded(page, "#ledger-table")
    assert ids(page) == list(range(101, 121))
    assert query_of(page.url) == {"page": "3"}
    assert pages == ["9", "3"]
    expect(page.locator("#page-next")).to_be_disabled()
    expect(page.locator("#page-prev")).to_be_enabled()


def _fail_500(route) -> None:  # noqa: ANN001
    route.fulfill(status=500, content_type="application/json",
                  body='{"error": {"code": "internal_error", "message": "測試用的 500"}}')


@pytest.mark.expect_http_errors(500)
def test_export_link_disabled_when_list_fails(new_page: NewPage, gw: LiveGateway) -> None:
    insert_ledger(gw, 1, NOW - timedelta(hours=1), deployment="m1")
    page = new_page()
    page.route(lambda url: urlsplit(url).path == "/api/ledger", _fail_500)
    goto(page, gw, "/ui/ledger.html?status=error")
    wait_loaded(page, "#ledger-table")
    expect(page.locator("#msgbar")).to_contain_text("測試用的 500")
    export = page.locator("#export-csv")
    expect(export).to_have_attribute("aria-disabled", "true")
    assert export.get_attribute("href") is None
    expect(page.locator("#page-next")).to_be_disabled()
    # 改篩選後仍失敗:連結保持停用,不沿用舊篩選
    with page.expect_request(is_list):
        page.locator("#f-route").select_option("rt")
    wait_loaded(page, "#ledger-table")
    expect(export).to_have_attribute("aria-disabled", "true")
    assert export.get_attribute("href") is None


@pytest.mark.expect_http_errors(500)
def test_member_list_failure_does_not_mark_orphans(new_page: NewPage, gw: LiveGateway) -> None:
    insert_ledger(gw, 1, NOW - timedelta(hours=1), deployment="m1")
    insert_ledger(gw, 2, NOW - timedelta(hours=2), deployment="ghost")
    page = new_page()
    page.route(lambda url: urlsplit(url).path == "/api/deployments", _fail_500)
    goto(page, gw, "/ui/ledger.html")
    wait_loaded(page, "#ledger-table")
    expect(page.locator("#msgbar")).to_contain_text("測試用的 500")
    cell = lambda i: page.locator(f'#ledger-body tr[data-ledger-id="{i}"] td.ledger-member-cell')  # noqa: E731
    expect(cell(1)).to_have_text("m1")
    expect(cell(2)).to_have_text("ghost")
    expect(page.locator(".ledger-orphan")).to_have_count(0)
    # route 選單照常載入
    expect(page.locator("#f-route option")).to_have_text(["全部", "other", "rt"])

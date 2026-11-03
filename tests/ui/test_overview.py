"""UI-1 總覽(CHG-20261103-01 T4 L2;決策 11、18、20;設計差異 #2、#3)。

每個成員一列:剩餘額度(與 `/api/dashboard` 相同,`$x.xx`;`credit_pool` 也以 USD)、冷卻至本地時間與原因、
`unlimited` 為「不限」、預估天數、近 7 天日均、近 24 小時請求數;摘要卡與 `/api/dashboard` 相符;清除冷卻;
成員 id 連到 `members.html#<id>`(encodeURIComponent);`first_run` 導向精靈、`?skip_wizard=1` 不導向;
有成員無 route 時顯示續作橫幅;tags、帳本 error 含 HTML 時以文字顯示。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Page, expect

from spillway.clock import FakeClock
from tests.helpers.live_gateway import LiveGateway
from tests.helpers.ui import MakeGw, NewPage, add_member, add_route, api, goto, guard_of, wait_loaded
from tests.helpers.ui_ledger import insert_ledger
from tests.helpers.ui_overview import set_cooldown


NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
TPE = ZoneInfo("Asia/Taipei")
XSS_IMG = "<img src=x onerror=alert(1)>"
XSS_SCRIPT = "<script>alert(2)</script>"


def local(dt: datetime) -> str:
    return dt.astimezone(TPE).strftime("%Y/%m/%d %H:%M:%S")


def usd(x: float) -> str:
    return f"${x:.2f}"


def row(page: Page, dep_id: str):  # noqa: ANN201
    return page.locator(f'#members-body tr[data-member="{dep_id}"]')


@pytest.fixture
def gw(make_ui_gateway: MakeGw) -> LiveGateway:
    return make_ui_gateway(clock=FakeClock(NOW))


@pytest.fixture
def seeded(gw: LiveGateway) -> LiveGateway:
    add_member(gw, "a-usd", budget={"type": "usd", "limit_amount": 10, "window": "monthly", "reset_anchor": 1})
    add_member(gw, "c-pool", budget={"type": "credit_pool", "limit_amount": 100, "window": "monthly",
                                     "reset_anchor": 1})
    add_member(gw, "d-cool", budget={"type": "usd", "limit_amount": 2, "window": "daily"})
    add_member(gw, "u-free", kind="local")
    add_route(gw, "rt")
    n = 1
    for dep, cost, ago in [("a-usd", 1.5, timedelta(days=1, hours=1)), ("a-usd", 0.25, timedelta(hours=2)),
                           ("c-pool", 36.8, timedelta(days=3)), ("d-cool", 0.75, timedelta(hours=3)),
                           ("u-free", 0.0, timedelta(hours=1)), ("a-usd", None, timedelta(hours=1))]:
        insert_ledger(gw, n, NOW - ago, deployment=dep, cost_usd=cost, status="ok" if cost is not None else "error")
        n += 1
    set_cooldown(gw, "d-cool", NOW + timedelta(minutes=90), "rate_limited")
    return gw


def test_rows_and_summary_match_dashboard(new_page: NewPage, seeded: LiveGateway) -> None:
    gw = seeded
    dash = {d["id"]: d for d in api(gw, "GET", "/api/dashboard").json()["deployments"]}
    page = new_page()
    goto(page, gw, "/ui/")
    wait_loaded(page, "#members-table")
    expect(page.locator("#members-body tr")).to_have_count(len(dash))
    assert page.locator("#members-body tr").evaluate_all("rs => rs.map(r => r.dataset.member)") == list(dash)

    for dep_id, d in dash.items():
        r = row(page, dep_id)
        if d["unlimited"]:
            expect(r.locator('[data-field="remaining"]')).to_have_text("不限")
        else:
            expect(r.locator('[data-field="remaining"]')).to_have_text(usd(d["remaining"]))
        expect(r.locator('[data-field="daily"]')).to_have_text(usd(d["daily_spend_7d"]))
        expect(r.locator('[data-field="requests"]')).to_have_text(str(d["requests_24h"]))
        days = "—" if d["est_days_left"] is None else f"{math.floor(d['est_days_left'])} 天"
        expect(r.locator("td").nth(5)).to_have_text(days)

    # credit_pool 以 USD 顯示;數值與 /api/dashboard 相同
    assert dash["c-pool"]["budget_type"] == "credit_pool"
    expect(row(page, "c-pool").locator('[data-field="remaining"]')).to_have_text(usd(100 - 36.8))
    expect(row(page, "c-pool").locator("td").nth(2)).to_contain_text("點數池(USD)")
    expect(row(page, "u-free").locator('[data-field="remaining"]')).to_have_text("不限")
    expect(row(page, "a-usd").locator('[data-field="remaining"]')).to_have_text("$8.25")

    # 冷卻中:冷卻至本地時間(Asia/Taipei)與原因,UTC 在 title;有「清除冷卻」
    cool = row(page, "d-cool").locator('[data-field="cooling"]')
    until = NOW + timedelta(minutes=90)
    expect(cool).to_have_text(f"冷卻至 {local(until)}(限流)")
    expect(cool.locator("time")).to_have_attribute("title", "2026-10-30T13:30:00Z (UTC)")
    expect(row(page, "d-cool").get_by_role("button", name="清除冷卻")).to_have_count(1)
    expect(page.get_by_role("button", name="清除冷卻")).to_have_count(1)

    # 摘要卡
    expect(page.locator("#stat-cooling")).to_have_text(str(sum(1 for d in dash.values() if d["cooling"])))
    expect(page.locator("#stat-cooling")).to_have_text("1")
    expect(page.locator("#stat-cooling-note")).to_contain_text("d-cool")
    total = sum(d["requests_24h"] for d in dash.values())
    expect(page.locator("#stat-requests")).to_have_text(str(total))
    assert total == 4      # a-usd 2(ok、error;1 天前不算)、d-cool 1、u-free 1
    expect(page.locator("#resume-banner")).to_be_hidden()


def test_usage_meter_matches_dashboard(new_page: NewPage, seeded: LiveGateway) -> None:
    gw = seeded
    dash = {d["id"]: d for d in api(gw, "GET", "/api/dashboard").json()["deployments"]}
    page = new_page()
    goto(page, gw, "/ui/")
    wait_loaded(page, "#members-table")
    for dep_id, d in dash.items():
        meter = row(page, dep_id).locator("meter")
        if d["unlimited"]:
            expect(meter).to_have_count(0)
            continue
        expect(meter).to_have_count(1)
        assert float(meter.get_attribute("min") or "nan") == 0
        assert float(meter.get_attribute("max") or "nan") == pytest.approx(d["limit_amount"])
        assert float(meter.get_attribute("value") or "nan") == pytest.approx(d["period_spend"])
        pct = round(d["period_spend"] / d["limit_amount"] * 100)
        expect(meter).to_have_attribute("aria-label", f"已使用 {pct}%")
    assert dash["u-free"]["unlimited"] is True
    expect(row(page, "u-free").locator("meter")).to_have_count(0)
    expect(row(page, "c-pool").locator("meter")).to_have_attribute("aria-label", "已使用 37%")


def test_clear_cooldown(new_page: NewPage, seeded: LiveGateway) -> None:
    gw = seeded
    page = new_page()
    goto(page, gw, "/ui/")
    wait_loaded(page, "#members-table")
    with page.expect_response(lambda r: r.url.endswith("/api/deployments/d-cool/clear-cooldown")) as resp:
        row(page, "d-cool").get_by_role("button", name="清除冷卻").click()
    assert resp.value.status == 200 and resp.value.request.method == "POST"
    expect(row(page, "d-cool").locator('[data-field="cooling"]')).to_have_count(0)
    expect(row(page, "d-cool").locator("td").nth(8)).to_have_text("正常")
    expect(page.get_by_role("button", name="清除冷卻")).to_have_count(0)
    expect(page.locator("#stat-cooling")).to_have_text("0")
    dash = {d["id"]: d for d in api(gw, "GET", "/api/dashboard").json()["deployments"]}
    assert dash["d-cool"]["cooling"] is False


def test_member_id_links_to_members_page(new_page: NewPage, gw: LiveGateway) -> None:
    odd = "a#b?c%d&e"
    add_member(gw, odd, kind="local")
    add_member(gw, "plain", kind="local")
    add_route(gw, "rt")
    page = new_page()
    goto(page, gw, "/ui/")
    wait_loaded(page, "#members-table")
    link = page.locator('#members-body a.overview-member-link').filter(has_text=odd)
    expect(link).to_have_attribute("href", "/ui/members.html#" + quote(odd, safe=""))
    assert quote(odd, safe="") == "a%23b%3Fc%25d%26e"
    expect(page.locator('#members-body a.overview-member-link').filter(has_text="plain")).to_have_attribute(
        "href", "/ui/members.html#plain")
    link.click()
    expect(page).to_have_url(gw.url("/ui/members.html#a%23b%3Fc%25d%26e"))


def test_first_run_redirects_to_wizard(new_page: NewPage, gw: LiveGateway) -> None:
    assert api(gw, "GET", "/api/settings").json()["first_run"] is True
    page = new_page()
    page.goto(gw.url("/ui/"))
    expect(page).to_have_url(gw.url("/ui/wizard.html"))
    page.goto(gw.url("/"))
    expect(page).to_have_url(gw.url("/ui/wizard.html"))
    methods = {m for m, _ in guard_of(page).requests}
    assert methods == {"GET"}


def test_skip_wizard_stays_on_overview(new_page: NewPage, gw: LiveGateway) -> None:
    page = new_page()
    goto(page, gw, "/ui/?skip_wizard=1")
    wait_loaded(page, "#members-table")
    expect(page).to_have_url(gw.url("/ui/?skip_wizard=1"))
    expect(page.locator("main h1")).to_have_text("總覽")
    expect(page.locator("#members-body")).to_have_text("尚未加入成員")
    expect(page.locator("#resume-banner")).to_be_hidden()
    expect(page.locator("#stat-cooling")).to_have_text("0")
    expect(page.locator("#stat-requests")).to_have_text("0")


def test_resume_banner_when_members_but_no_routes(new_page: NewPage, gw: LiveGateway) -> None:
    add_member(gw, "only", kind="local")
    page = new_page()
    goto(page, gw, "/ui/")
    wait_loaded(page, "#members-table")
    expect(page).to_have_url(gw.url("/ui/"))           # 有成員 → first_run 為 false,不導向
    banner = page.locator("#resume-banner")
    expect(banner).to_be_visible()
    expect(banner).to_have_text("尚未建立 route — 繼續首次設定")
    expect(banner).to_have_attribute("href", "/ui/wizard.html")
    banner.click()
    expect(page).to_have_url(gw.url("/ui/wizard.html"))
    add_route(gw, "rt")
    goto(page, gw, "/ui/")
    wait_loaded(page, "#members-table")
    expect(page.locator("#resume-banner")).to_be_hidden()


def test_html_in_user_data_is_text(new_page: NewPage, gw: LiveGateway) -> None:
    add_member(gw, "xss", kind="local", tags=[XSS_IMG, XSS_SCRIPT])
    add_route(gw, "rt", tag=XSS_IMG)
    page = new_page()
    goto(page, gw, "/ui/")
    wait_loaded(page, "#members-table")
    chips = row(page, "xss").locator(".chip")
    expect(chips).to_have_text([XSS_IMG, XSS_SCRIPT])
    assert page.locator("main img").count() == 0
    assert page.locator("script").count() == 1          # 只有頁面自己的 module script
    assert page.evaluate("() => document.querySelectorAll('img').length") == 0
    assert guard_of(page).dialogs == []

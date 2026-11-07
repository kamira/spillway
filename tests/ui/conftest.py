"""畫面測試的 fixture(CHG-20261103-01 T3;決策 3、22,決策審議修訂 AM-5)。

(CHG-20261107-03 T5 黑箱化)gateway 改為 `tests.blackbox.harness.Serve`(`SPILLWAY_CMD serve` 子行程);
本目錄只 import `tests.blackbox`(與 Playwright),不 import spillway(邊界守門與執行期守門)。

不用 pytest-playwright;一律 Playwright **sync** API。Playwright sync API 會把主 thread 標成「有執行中的
事件迴圈」,所以 `playwright` 與 `browser` 是 **module 範圍**,模組結束時 `browser.close()` 與
`playwright.stop()`;autouse 的 module 範圍 teardown 斷言 `asyncio._get_running_loop() is None`。
本目錄的測試在主 thread 不可使用 `asyncio.run`、anyio、`async def` 測試、`gw.async_http`、`mcp_session`;
要查伺服端狀態時,用同步的 `gw.http()`、`gw.control`(測試控制介面)或 `gw.db()`(唯讀)。

Fixture
- `playwright`、`browser`(module):headless Chromium(Playwright 1.64.1 對應的已快取 1248;不存在時
  啟動失敗 → 測試失敗,不 skip)。
- `make_ui_gateway(**opts) -> Serve`(函式範圍):經 `tests/blackbox/plugin.serve_factory` 啟動 serve 子行程
  (資料目錄在 `tmp_path`;開啟測試控制介面)並等到就緒;`opts` 為 `Serve` 的選項(`start_time`、
  `adapter_paths`、`allowed_unavailable_adapters`、`no_token` 等)。`retention_poll_seconds` 預設 3600
  (背景清理不干擾測試,與原行程內 gateway 相同)。子行程的環境在啟動時決定,所以啟動 serve 前,先建立
  測試用到的每一個會設定環境的 fixture(`ENV_FIXTURES`:`fake_api` 設 `SPILLWAY_TEST_KEY`、`fake_cli` 把假
  CLI 放上 PATH、`fake_key_env`);新增這類 fixture 時必須加進 `ENV_FIXTURES`(施工審議 T5 F4)。測試結束時停止(`Serve.stop()` 的結束碼與 stderr 斷言;
  測試失敗時改為 kill)。
- `fake_api`:沿用 `tests/fakes/plugin.py`(伺服器為 session 範圍,行程內;不 import spillway),另設
  `SPILLWAY_TEST_KEY` 為假金鑰(`tests.blackbox.mask.TEST_KEY`)。
- 假 CLI:根 conftest 的 `fake_cli`(假 `claude`、`codex` 放到 PATH 最前面)直接可用。
- `new_page(*, permissions=(), accept_downloads=True, timezone_id="Asia/Taipei", locale="zh-TW") -> Page`:
  每次一個新的 context(1280×800);攔截並記錄非本機請求;收集 console error 與 `pageerror`,測試結束時
  有任何一筆即失敗(見 `tests/blackbox/ui.PageGuard`;`guard_of(page)` 取得)。預設 dialog 處理器是
  「記錄後 dismiss」;`beforeunload` 的案例由測試自行處理(先以 click 製造變更,再在
  `with guard_of(page).manual_dialogs():` 內用 `page.once("dialog", …)` 斷言 type 後 accept)。
- marker `expect_http_errors(*statuses)`:只略過同源 `/api/` 且狀態碼在清單內的「Failed to load resource」
  console 訊息;其他 console error、CSP 違規、`pageerror` 照樣判失敗。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from tests.blackbox.harness import Serve
from tests.blackbox.mask import TEST_KEY, TEST_KEY_ENV
from tests.blackbox.ui import PageGuard, close_and_collect, open_context

# 會設定環境變數的 fixture:make_ui_gateway 啟動 serve 前先建立(子行程的環境在啟動時決定)
ENV_FIXTURES = ("fake_api", "fake_cli", "fake_key_env")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "expect_http_errors(*statuses): 略過同源 /api/ 且狀態碼在清單內的 Failed to load resource console 訊息")


@pytest.fixture(scope="module", autouse=True)
def _no_event_loop_leak() -> Iterator[None]:
    """module 結束(Playwright 已 stop)後,主 thread 不可留下「執行中的事件迴圈」標記。"""
    yield
    assert asyncio._get_running_loop() is None, "Playwright 的事件迴圈標記外洩到主 thread"


@pytest.fixture(scope="module")
def playwright() -> Iterator[Playwright]:
    pw = sync_playwright().start()
    try:
        yield pw
    finally:
        pw.stop()


@pytest.fixture(scope="module")
def browser(playwright: Playwright) -> Iterator[Browser]:
    b = playwright.chromium.launch(headless=True)
    try:
        yield b
    finally:
        b.close()


@pytest.fixture
def make_ui_gateway(serve_factory: Callable[..., Serve],
                    request: pytest.FixtureRequest) -> Iterator[Callable[..., Serve]]:
    def make(**opts: Any) -> Serve:
        for name in ENV_FIXTURES:
            if name in request.fixturenames:
                request.getfixturevalue(name)   # 先設好環境(金鑰、PATH),子行程才看得到
        opts.setdefault("retention_poll_seconds", 3600)
        return serve_factory(**opts)

    yield make   # 停止由 serve_factory 收尾(相反順序;已由測試自行停止的不重複)


@pytest.fixture
def fake_api(fake_api: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY)
    return fake_api


@pytest.fixture
def new_page(browser: Browser, request: pytest.FixtureRequest,
             make_ui_gateway: Callable[..., Serve]) -> Iterator[Callable[..., Page]]:
    # 依賴 make_ui_gateway:gateway 在分頁關閉之後才停止,避免停機造成的連線錯誤被記為違規
    marker = request.node.get_closest_marker("expect_http_errors")
    allowed = tuple(int(s) for s in marker.args) if marker else ()
    opened: list[tuple[BrowserContext, PageGuard]] = []

    def factory(*, permissions: tuple[str, ...] | list[str] = (), accept_downloads: bool = True,
                timezone_id: str = "Asia/Taipei", locale: str = "zh-TW") -> Page:
        ctx, guard = open_context(browser, permissions=permissions, accept_downloads=accept_downloads,
                                  timezone_id=timezone_id, locale=locale, allowed_statuses=allowed)
        opened.append((ctx, guard))
        return ctx.new_page()

    yield factory
    # 先關閉再收集(T3 審議 M2):最後一個動作之後才發生的事件要到 close 時才送達
    problems = [str(v) for v in close_and_collect(opened)]
    if problems:
        pytest.fail("畫面違規(console error / pageerror / 非本機請求):\n" + "\n".join(problems))

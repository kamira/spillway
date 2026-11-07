"""畫面測試的 fixture(CHG-20261103-01 T3;決策 3、22,決策審議修訂 AM-5)。

不用 pytest-playwright;一律 Playwright **sync** API。Playwright sync API 會把主 thread 標成「有執行中的
事件迴圈」,所以 `playwright` 與 `browser` 是 **module 範圍**,模組結束時 `browser.close()` 與
`playwright.stop()`;autouse 的 module 範圍 teardown 斷言 `asyncio._get_running_loop() is None`。
本目錄的測試在主 thread 不可使用 `asyncio.run`、anyio、`async def` 測試、`gw.async_http`、`mcp_session`;
要查伺服端狀態時,用同步的 `gw.http()` 或 `gw.call_in_loop`。

Fixture
- `playwright`、`browser`(module):headless Chromium(Playwright 1.64.1 對應的已快取 1248;不存在時
  啟動失敗 → 測試失敗,不 skip)。
- `make_ui_gateway(db_path=None, **create_app_kwargs) -> LiveGateway`(函式範圍):以
  `tests/helpers/live_gateway.start_live_gateway` 在背景 thread 跑真的 uvicorn;資料目錄預設在
  `tmp_path`;可傳 `clock`、`extra_adapter_paths` 等 `create_app` 參數;測試結束時停止。
- `fake_api`:沿用 `tests/fakes/plugin.py`(伺服器為 session 範圍),另設 `SPILLWAY_TEST_KEY` 為假金鑰
  (`tests.helpers.TEST_KEY`)。
- 假 CLI:根 conftest 的 `fake_cli`(假 `claude`、`codex` 放到 PATH 最前面)直接可用。
- `new_page(*, permissions=(), accept_downloads=True, timezone_id="Asia/Taipei", locale="zh-TW") -> Page`:
  每次一個新的 context(1280×800);攔截並記錄非本機請求;收集 console error 與 `pageerror`,測試結束時
  有任何一筆即失敗(見 `tests/helpers/ui.PageGuard`;`guard_of(page)` 取得)。預設 dialog 處理器是
  「記錄後 dismiss」;`beforeunload` 的案例由測試自行處理(先以 click 製造變更,再在
  `with guard_of(page).manual_dialogs():` 內用 `page.once("dialog", …)` 斷言 type 後 accept)。
- marker `expect_http_errors(*statuses)`:只略過同源 `/api/` 且狀態碼在清單內的「Failed to load resource」
  console 訊息;其他 console error、CSP 違規、`pageerror` 照樣判失敗。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from tests.helpers import TEST_KEY, TEST_KEY_ENV
from tests.helpers.live_gateway import LiveGateway, start_live_gateway
from tests.helpers.ui import PageGuard, close_and_collect, open_context


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
def make_ui_gateway(tmp_path: Path) -> Iterator[Callable[..., LiveGateway]]:
    started: list[LiveGateway] = []

    def make(db_path: Path | str | None = None, **create_app_kwargs: Any) -> LiveGateway:
        path = Path(db_path) if db_path is not None else tmp_path / f"ui-data-{len(started)}" / "spillway.db"
        gw = start_live_gateway(path, **create_app_kwargs)
        started.append(gw)
        return gw

    yield make
    errors: list[BaseException] = []
    for gw in reversed(started):
        try:
            gw.stop()
        except BaseException as exc:  # noqa: BLE001 — 全部停完再回報
            errors.append(exc)
    if errors:
        raise errors[0]


@pytest.fixture
def fake_api(fake_api: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY)
    return fake_api


@pytest.fixture
def new_page(browser: Browser, request: pytest.FixtureRequest,
             make_ui_gateway: Callable[..., LiveGateway]) -> Iterator[Callable[..., Page]]:
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

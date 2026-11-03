"""畫面測試共用的頁面操作與種資料層(CHG-20261103-01 T3;決策 22,決策審議修訂 AM-5)。

`tests/ui/conftest.py` 與 T9 的 behave 步驟都只經本檔(與各 task 的 `tests/helpers/ui_<area>.py`)操作畫面;
本檔只用 Playwright **sync** API 與同步 httpx,不在主 thread 使用 asyncio(Global Constraints)。

常數
- `NAV`:導覽 8 項(文字, href),依決策 20。
- `PAGES`:10 個頁面 → `Page(file, path, title, current, script)`。

瀏覽器
- `open_context(browser, *, permissions=(), accept_downloads=True, timezone_id="Asia/Taipei",
  locale="zh-TW", allowed_statuses=()) -> (BrowserContext, PageGuard)`:新的 context(1280×800),
  非本機請求一律 abort 並記錄;之後開的每個分頁都掛上 guard。
- `PageGuard`:收集 console 的 error、`pageerror`、非本機請求與 dialog。
  - console error 中只有「Failed to load resource」且資源為**同源 `/api/`**、狀態碼在 `allowed_statuses`
    內者略過(`expect_http_errors` marker);其他 console error(含 CSP 違規)與 `pageerror` 一律記為違規。
  - 預設 dialog 處理器「記錄後 dismiss」;`beforeunload` 等需要自行處理的案例,在 `with guard.manual_dialogs():`
    內以 `page.once("dialog", …)` 自行 accept/dismiss。
  - `violations()`:目前的違規清單;`take(pred)`:移除並回傳符合的違規(測試已明確斷言的預期錯誤,例如
    gateway 停止後的 `net::ERR_*`);`assert_clean()`。
- `guard_of(page) -> PageGuard`。
- `goto(page, gw, path)`:開啟頁面並等到共用外框初始化(`html[data-shell="ready"]`)。
- `import_module(page, name)`:在頁面中動態 import `assets/js/<name>`,存到 `window.__mods[name]`
  (之後以 `page.evaluate("… window.__mods['api.js'] …")` 呼叫)。

種資料與 DB(單一 adapter 層)
- `api(gw, method, path, json=None, *, expect=(200, 201, 204)) -> httpx.Response`:優先經 `/api` 種資料。
- `db_snapshot(gw) -> dict[str, list[tuple]]`:全表快照(比對「不寫入」用)。
- `db_execute(gw, sql, params=())`:非經 `/api` 不可的寫入(例如種入帳本列)集中在這裡。
"""
from __future__ import annotations

import contextlib
import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from playwright.sync_api import Browser, BrowserContext, ConsoleMessage, Dialog, Error, Page, Request, Route

__all__ = ["NAV", "PAGES", "Page_", "PageGuard", "api", "db_execute", "db_snapshot", "goto", "guard_of",
           "import_module", "is_local_url", "open_context"]

NAV: list[tuple[str, str]] = [
    ("總覽", "/ui/"), ("成員", "/ui/members.html"), ("群組與 Tag", "/ui/groups.html"),
    ("Route", "/ui/routes.html"), ("MCP 設定", "/ui/mcp.html"), ("帳本", "/ui/ledger.html"),
    ("設定", "/ui/settings.html"), ("API 參考", "/ui/api-reference.html"),
]


@dataclass(frozen=True)
class Page_:
    file: str
    title: str
    current: str      # aria-current 指向的導覽文字
    script: str

    @property
    def path(self) -> str:
        return "/ui/" if self.file == "index.html" else f"/ui/{self.file}"


PAGES: list[Page_] = [
    Page_("index.html", "總覽", "總覽", "overview.js"),
    Page_("members.html", "成員", "成員", "members.js"),
    Page_("groups.html", "群組與 Tag", "群組與 Tag", "groups.js"),
    Page_("routes.html", "Route", "Route", "routes.js"),
    Page_("route.html", "編輯 Route", "Route", "route-editor.js"),
    Page_("mcp.html", "MCP 設定", "MCP 設定", "mcp.js"),
    Page_("ledger.html", "帳本", "帳本", "ledger.js"),
    Page_("settings.html", "設定", "設定", "settings.js"),
    Page_("wizard.html", "首次設定", "總覽", "wizard.js"),
    Page_("api-reference.html", "API 參考", "API 參考", "api-reference.js"),
]

_LOCAL_HOSTS = {"127.0.0.1", "localhost"}
_FAILED_STATUS = re.compile(r"^Failed to load resource: the server responded with a status of (\d{3})")


def is_local_url(url: str) -> bool:
    """本機請求:`http(s)://127.0.0.1|localhost`、同源的 `blob:`、`about:`。"""
    parts = urlsplit(url)
    if parts.scheme in ("about",):
        return True
    if parts.scheme == "blob":
        return is_local_url(url[len("blob:"):])
    return parts.scheme in ("http", "https") and (parts.hostname or "") in _LOCAL_HOSTS


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


@dataclass
class Violation:
    kind: str          # console | pageerror | request
    text: str
    url: str = ""

    def __str__(self) -> str:
        return f"[{self.kind}] {self.text}" + (f" ({self.url})" if self.url else "")


@dataclass
class PageGuard:
    allowed_statuses: frozenset[int] = frozenset()
    records: list[Violation] = field(default_factory=list)
    dialogs: list[tuple[str, str]] = field(default_factory=list)
    requests: list[tuple[str, str]] = field(default_factory=list)   # (method, url),所有分頁
    _manual: int = 0

    # ---- 掛載 ----
    def attach(self, page: Page) -> None:
        page.on("console", lambda msg: self._on_console(page, msg))
        page.on("pageerror", lambda err: self.records.append(Violation("pageerror", str(err))))
        page.on("dialog", self._on_dialog)
        page.on("request", self._on_request)
        _GUARDS[id(page)] = self

    def _on_console(self, page: Page, msg: ConsoleMessage) -> None:
        if msg.type != "error":
            return
        url = (msg.location or {}).get("url", "") or ""
        m = _FAILED_STATUS.match(msg.text)
        if m and int(m.group(1)) in self.allowed_statuses and url:
            try:
                same_origin = _origin(url) == _origin(page.url)
            except Exception:  # noqa: BLE001 — 分頁已關閉時無法取得 url
                same_origin = False
            if same_origin and urlsplit(url).path.startswith("/api/"):
                return
        self.records.append(Violation("console", msg.text, url))

    def _on_request(self, req: Request) -> None:
        self.requests.append((req.method, req.url))
        if not is_local_url(req.url):
            self.records.append(Violation("request", f"非本機請求 {req.method}", req.url))

    def block(self, route: Route) -> None:
        """`context.route` 的處理器:非本機請求一律 abort(違規由 `_on_request` 記錄)。"""
        route.abort()

    def _on_dialog(self, dialog: Dialog) -> None:
        self.dialogs.append((dialog.type, dialog.message))
        if self._manual:
            return
        with contextlib.suppress(Error):
            dialog.dismiss()

    @contextlib.contextmanager
    def manual_dialogs(self) -> Iterator[None]:
        """區塊內預設處理器只記錄、不 dismiss;測試以 `page.once("dialog", …)` 自行處理。"""
        self._manual += 1
        try:
            yield
        finally:
            self._manual -= 1

    # ---- 查詢 ----
    def violations(self) -> list[Violation]:
        return list(self.records)

    def take(self, pred: Callable[[Violation], bool]) -> list[Violation]:
        taken = [v for v in self.records if pred(v)]
        self.records = [v for v in self.records if not pred(v)]
        return taken

    def assert_clean(self) -> None:
        assert self.records == [], "畫面違規:\n" + "\n".join(map(str, self.records))


_GUARDS: dict[int, PageGuard] = {}


def guard_of(page: Page) -> PageGuard:
    return _GUARDS[id(page)]


def open_context(browser: Browser, *, permissions: Iterable[str] = (), accept_downloads: bool = True,
                 timezone_id: str = "Asia/Taipei", locale: str = "zh-TW",
                 allowed_statuses: Iterable[int] = ()) -> tuple[BrowserContext, PageGuard]:
    ctx = browser.new_context(permissions=list(permissions), accept_downloads=accept_downloads,
                              timezone_id=timezone_id, locale=locale,
                              viewport={"width": 1280, "height": 800})
    ctx.set_default_timeout(10_000)
    guard = PageGuard(allowed_statuses=frozenset(allowed_statuses))
    ctx.route(lambda url: not is_local_url(url), guard.block)
    ctx.on("page", guard.attach)
    return ctx, guard


def goto(page: Page, gw: Any, path: str, *, wait_until: str = "load") -> None:
    page.goto(gw.url(path), wait_until=wait_until)
    page.wait_for_selector('html[data-shell="ready"]', state="attached")


def import_module(page: Page, name: str) -> list[str]:
    """動態 import `/ui/assets/js/<name>`(同源,CSP `script-src 'self'` 允許),回傳匯出名稱。"""
    return page.evaluate(
        """async (name) => {
             const m = await import(`/ui/assets/js/${name}`);
             window.__mods = window.__mods || {};
             window.__mods[name] = m;
             return Object.keys(m).sort();
           }""", name)


# ---- 種資料與 DB(單一 adapter 層)--------------------------------------------------------------

def api(gw: Any, method: str, path: str, json: Any = None, *,
        expect: Iterable[int] = (200, 201, 204)) -> httpx.Response:
    with gw.http() as client:
        resp = client.request(method, path, json=json)
    assert resp.status_code in tuple(expect), (method, path, resp.status_code, resp.text)
    return resp


def db_snapshot(gw: Any) -> dict[str, list[tuple[Any, ...]]]:
    with gw.db() as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {t: sorted((tuple(r) for r in conn.execute(f'SELECT * FROM "{t}"')), key=repr) for t in tables}


def db_execute(gw: Any, sql: str, params: Iterable[Any] = ()) -> None:
    with gw.db() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(sql, tuple(params))
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise

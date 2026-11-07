"""畫面測試共用的頁面操作與種資料層(CHG-20261103-01 T3;決策 22,決策審議修訂 AM-5)。

(CHG-20261107-03 T2 由 `tests/helpers/ui.py` 複製改寫成黑箱版;原檔不動,T5 改用本模組後刪除)
黑箱版:`gw` 為 `tests.blackbox.harness.Serve`(子行程 serve);資料庫只唯讀(`gw.db()`),非經 `/api` 不可的
寫入一律經測試控制介面(`gw.control`,spec 13);不 import spillway。

`tests/ui/conftest.py` 與 behave 步驟都只經本套件(與 `tests/blackbox/ui/<area>.py`)操作畫面;
本檔只用 Playwright **sync** API 與同步 httpx,不在主 thread 使用 asyncio(Global Constraints)。

常數
- `NAV`:導覽 8 項(文字, href),依決策 20。
- `PAGES`:10 個頁面 → `PageSpec(file, title, current, script)`(`path` 為推導屬性)。

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
- `close_and_collect(opened) -> list[Violation]`:**先**關閉每個 context(讓排隊中的 console/pageerror
  事件送達),再收集違規;只濾掉關閉時被中止的請求產生的 `net::ERR_ABORTED`(T3 審議 M2)。
- `goto(page, gw, path)`:開啟頁面並等到共用外框初始化(`html[data-shell="ready"]`)。
- `import_module(page, name)`:在頁面中動態 import `assets/js/<name>`,存到 `window.__mods[name]`
  (之後以 `page.evaluate("… window.__mods['api.js'] …")` 呼叫)。
- `wait_loaded(page, selector)`:等到 `selector` 的元素帶 `data-loaded="true"`(頁面完成一次載入)。
- `query_of(url) -> dict[str, str]`:網址的 query(單值)。
- `CLIP`:剪貼簿讀寫權限(`new_page(permissions=CLIP)`)。
- `confirm_dialog(page) -> Locator`:`ui.confirmDialog()` 開出的確認對話框(以 `#confirm-dialog-title` 辨識)。
- `drag(page, src, dst)`:HTML5 拖曳(先把來源與目標一起捲進視窗再 `drag_to`;AM-9)。
- 型別別名:`NewPage`(`new_page` fixture)、`MakeGw`(回傳 `Serve` 的 gateway 工廠 fixture)。

種資料與 DB(單一 adapter 層)
- `api(gw, method, path, json=None, *, expect=(200, 201, 204)) -> httpx.Response`:優先經 `/api` 種資料。
- `db_snapshot(gw) -> dict[str, list[tuple]]`:全表快照(唯讀連線;比對「不寫入」用)。
- 原 `db_execute`(任意 SQL 寫入)不提供:帳本、內容、冷卻與損壞的成員 params 一律經 `gw.control`
  (`tests/blackbox/ui/ledger.py`、`overview.py`、`routes.py`;`gw.control.raw_deployment_params`)。
- `member_body(dep_id, *, kind="api", tags=None, budget=None, **kw)`:`POST /api/deployments` 的 body
  (api 成員的憑證參照為 `env:SPILLWAY_TEST_KEY`,只是參照,不含值;`kw` 覆寫任何欄位)。
- `add_member(gw, dep_id, **kw) -> dict`;`add_route(gw, name, tag="r", *, target=None, **kw) -> dict`
  (target 省略時為 `{"tags": {"any": [tag]}}`);`seed_group(gw, name, selector=None) -> dict`
  (selector 省略時為 `{"any": [name]}`)。
"""
from __future__ import annotations

import contextlib
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import httpx
from playwright.sync_api import (Browser, BrowserContext, ConsoleMessage, Dialog, Error, Locator, Page, Request,
                                 Route, expect)

from tests.blackbox.db import snapshot
from tests.blackbox.mask import TEST_KEY_ENV

if TYPE_CHECKING:
    from tests.blackbox.harness import Serve

__all__ = ["CLIP", "NAV", "PAGES", "MakeGw", "NewPage", "PageSpec", "PageGuard", "add_member", "add_route", "api",
           "db_snapshot", "close_and_collect", "confirm_dialog", "drag", "goto", "guard_of",
           "import_module", "is_local_url", "member_body", "open_context", "query_of", "seed_group", "wait_loaded"]

# fixture 的型別別名(tests/ui/conftest.py 的 new_page 與 gateway 工廠)
NewPage = Callable[..., Page]
MakeGw = Callable[..., "Serve"]

# 剪貼簿讀寫權限(new_page(permissions=CLIP))
CLIP: list[str] = ["clipboard-read", "clipboard-write"]

# 導覽 8 項:鏡像 web/static/assets/js/strings.js 的正式清單 NAV(決策 20);各 HTML 的 <nav> 須與此一致
NAV: list[tuple[str, str]] = [
    ("總覽", "/ui/"), ("成員", "/ui/members.html"), ("群組與 Tag", "/ui/groups.html"),
    ("Route", "/ui/routes.html"), ("MCP 設定", "/ui/mcp.html"), ("帳本", "/ui/ledger.html"),
    ("設定", "/ui/settings.html"), ("API 參考", "/ui/api-reference.html"),
]


@dataclass(frozen=True)
class PageSpec:
    file: str
    title: str
    current: str      # aria-current 指向的導覽文字
    script: str

    @property
    def path(self) -> str:
        return "/ui/" if self.file == "index.html" else f"/ui/{self.file}"


PAGES: list[PageSpec] = [
    PageSpec("index.html", "總覽", "總覽", "overview.js"),
    PageSpec("members.html", "成員", "成員", "members.js"),
    PageSpec("groups.html", "群組與 Tag", "群組與 Tag", "groups.js"),
    PageSpec("routes.html", "Route", "Route", "routes.js"),
    PageSpec("route.html", "編輯 Route", "Route", "route-editor.js"),
    PageSpec("mcp.html", "MCP 設定", "MCP 設定", "mcp.js"),
    PageSpec("ledger.html", "帳本", "帳本", "ledger.js"),
    PageSpec("settings.html", "設定", "設定", "settings.js"),
    PageSpec("wizard.html", "首次設定", "總覽", "wizard.js"),
    PageSpec("api-reference.html", "API 參考", "API 參考", "api-reference.js"),
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


_ABORT_NOISE = "net::ERR_ABORTED"


def close_and_collect(opened: Iterable[tuple[BrowserContext, PageGuard]]) -> list[Violation]:
    """先關閉 context 再收集違規:sync API 的事件只在下一次 Playwright 呼叫時派送,所以測試最後一個
    動作之後才發生的 console error 或 pageerror,要到 `close()` 時才會送達。關閉當下新增、屬於中止請求的
    `net::ERR_ABORTED` 不算違規(關閉造成的雜訊);其他一律保留。"""
    found: list[Violation] = []
    for ctx, guard in opened:
        before = len(guard.records)
        ctx.close()
        late = [v for v in guard.records[before:] if not (v.kind == "console" and _ABORT_NOISE in v.text)]
        found.extend(guard.records[:before] + late)
    return found


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


def wait_loaded(page: Page, selector: str) -> None:
    """等到 `selector` 的元素帶 `data-loaded="true"`。"""
    expect(page.locator(f'{selector}[data-loaded="true"]')).to_have_count(1)


def confirm_dialog(page: Page) -> Locator:
    """`ui.confirmDialog()` 開出的確認對話框。"""
    return page.locator("dialog").filter(has=page.locator("#confirm-dialog-title"))


def drag(page: Page, source: Locator, target: Locator) -> None:
    """HTML5 拖曳。拖曳目標在 dragstart 之前就必須可見且有尺寸(AM-9);拖曳中途捲動會讓 Chromium 取消拖曳,
    所以先把來源與目標一起捲進視窗(兩者的中點置中)再 `drag_to`。

    限制:來源頂端到目標底端的距離不可超過視窗高度(1280×800 的 context 為 800 px),否則兩者無法同時
    在視窗內,拖曳會失敗;需要更遠的移動時改用鍵盤替代(「移到…」選單、上移/下移)。"""
    expect(source).to_be_visible()
    expect(target).to_be_visible()
    src_box, dst_box = source.bounding_box(), target.bounding_box()
    assert src_box and dst_box and dst_box["height"] > 0 and dst_box["width"] > 0
    top = min(src_box["y"], dst_box["y"])
    bottom = max(src_box["y"] + src_box["height"], dst_box["y"] + dst_box["height"])
    page.evaluate("(mid) => window.scrollBy(0, mid - window.innerHeight / 2)", (top + bottom) / 2)
    source.drag_to(target)


def query_of(url: str) -> dict[str, str]:
    """網址的 query → dict;每個鍵必須只有一個值(否則 assert 失敗)。"""
    q = parse_qs(urlsplit(url).query, keep_blank_values=True)
    assert all(len(v) == 1 for v in q.values()), q
    return {k: v[0] for k, v in q.items()}


# ---- 種資料與 DB(單一 adapter 層)--------------------------------------------------------------

def api(gw: Any, method: str, path: str, json: Any = None, *,
        expect: Iterable[int] = (200, 201, 204)) -> httpx.Response:
    with gw.http() as client:
        resp = client.request(method, path, json=json)
    assert resp.status_code in tuple(expect), (method, path, resp.status_code, resp.text)
    return resp


def db_snapshot(gw: Any) -> dict[str, list[tuple[Any, ...]]]:
    with gw.db() as conn:
        return snapshot(conn)


# ---- 種資料:成員、route、群組 ---------------------------------------------------------------------

def member_body(dep_id: str, *, kind: str = "api", tags: list[str] | None = None,
                budget: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    """`POST /api/deployments` 的 body:api(openai)或 local(hosted_vllm)預設值,`kw` 覆寫任何欄位。"""
    body: dict[str, Any] = {"id": dep_id, "model_name": dep_id, "kind": kind, "tags": tags or ["r"]}
    if kind == "api":
        body.update(provider="openai", model="gpt-4o-mini", credential_ref=f"env:{TEST_KEY_ENV}",
                    capabilities={"ctx": 100000, "tools": True, "vision": True,
                                  "efforts": ["low", "medium", "high"]})
    else:
        body.update(provider="hosted_vllm", model="local-model",
                    capabilities={"ctx": 32768, "tools": False, "vision": False, "efforts": []})
    if budget is not None:
        body["budget"] = budget
    body.update(kw)
    return body


def add_member(gw: Any, dep_id: str, **kw: Any) -> dict[str, Any]:
    """以 `member_body(dep_id, **kw)` 建立成員(期望 201),回傳伺服器回應。"""
    return api(gw, "POST", "/api/deployments", member_body(dep_id, **kw), expect=(201,)).json()


def add_route(gw: Any, name: str, tag: str = "r", *, target: dict[str, Any] | None = None,
              **kw: Any) -> dict[str, Any]:
    """建立 route(期望 201);target 省略時為 `{"tags": {"any": [tag]}}`,`kw` 為其他欄位(mode 等)。"""
    body = {"name": name, "target": target if target is not None else {"tags": {"any": [tag]}}, **kw}
    return api(gw, "POST", "/api/routes", body, expect=(201,)).json()


def seed_group(gw: Any, name: str, selector: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """建立群組(期望 201);selector 省略時為 `{"any": [name]}`。"""
    body = {"name": name, "selector": selector if selector is not None else {"any": [name]}}
    return api(gw, "POST", "/api/groups", body, expect=(201,)).json()

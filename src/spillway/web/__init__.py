"""Web 畫面的靜態檔(CHG-20261103-01 T3;決策 5、6、7)。

畫面是 `static/` 下的 HTML、CSS 與 ES module JS,不經建置、不用模板;資料一律由瀏覽器以同源 `fetch`
呼叫 `/api/*` 取得。本模組只負責提供這些檔案,所以只 import 標準庫與 `starlette`(不 import
`spillway.api.admin`、`spillway.store`、`spillway.routing`;`tests/unit/test_web_assets.py` 檢查)。

`mount_web(app) -> None`:
- 以 `StaticFiles(directory=WEB_ROOT, html=True, follow_symlink=False)` 掛在 `/ui`(`/ui/` 即
  `index.html`;不列目錄;不放 `404.html`;指向根目錄外的 symlink 由 StaticFiles 拒絕);
- 外面包一層純 ASGI 檔案過濾 `StaticFilter`:路徑中任一段以 `.` 開頭(含 `..`、點檔)、含 `\\` 或
  NUL,或最後一段的副檔名不在 `{.html, .css, .js}` → 404(以 `HTTPException` 交給 app 的錯誤處理,
  spec 9.1.2 格式)。結尾為 `/` 的請求只有在根目錄下對應的相對路徑**確實是目錄**時才放行(交給
  `html=True` 處理 `index.html`);否則 404——StaticFiles 會 `normpath` 掉結尾斜線,`/ui/x.py/` 與
  `/ui/x.py%2f` 不擋就會送出 `x.py` 原文(T3 審議 M1);
- 只允許 GET、HEAD(其他方法由 StaticFiles 回 405);
- 註冊 `GET`/`HEAD /` → 302 `/ui/`(`include_in_schema=False`,不進 OpenAPI)。

安全標頭(CSP 等)由 `spillway.api.http_headers.SecurityHeadersMiddleware` 依路徑加上,不在這裡。
`WEB_ROOT` 在呼叫 `mount_web` 時才讀取(測試可換成暫存複本)。
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath
from collections.abc import Callable
from typing import Any

from starlette.exceptions import HTTPException
from starlette.responses import RedirectResponse
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

__all__ = ["ALLOWED_SUFFIXES", "StaticFilter", "WEB_ROOT", "is_allowed_path", "mount_web"]

WEB_ROOT: Path = Path(__file__).resolve().parent / "static"
ALLOWED_SUFFIXES = frozenset({".html", ".css", ".js"})
MOUNT_PATH = "/ui"


def is_allowed_path(path: str, is_dir: Callable[[str], bool] | None = None) -> bool:
    """`path` 為完整請求路徑(`scope["path"]`,已解碼)。結尾為 `/` 的目錄請求只在
    `is_dir(rel)` 為真時放行(`rel` 為去掉 `/ui` 前綴、以 `/` 分隔的相對路徑,根目錄為 "");
    `is_dir` 為 None 時一律拒絕目錄請求。"""
    segments = [s for s in path.split("/") if s]
    for seg in segments:
        if seg.startswith(".") or "\\" in seg or "\x00" in seg:
            return False
    if path.endswith("/"):
        rel_segments = segments[1:] if segments and "/" + segments[0] == MOUNT_PATH else segments
        return is_dir is not None and is_dir("/".join(rel_segments))
    if not segments:
        return False
    return PurePosixPath(segments[-1]).suffix in ALLOWED_SUFFIXES


class StaticFilter:
    """純 ASGI 檔案過濾;見模組說明。`root` 為靜態檔根目錄(判斷目錄請求用)。"""

    def __init__(self, app: ASGIApp, root: Path) -> None:
        self.app = app
        self.root = Path(root)

    def _is_dir(self, rel: str) -> bool:
        target = self.root.joinpath(*[s for s in rel.split("/") if s])
        return target.is_dir() and not target.is_symlink()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and not is_allowed_path(scope.get("path") or "", self._is_dir):
            raise HTTPException(status_code=404)
        await self.app(scope, receive, send)


async def _root_redirect() -> RedirectResponse:
    return RedirectResponse("/ui/", status_code=302)


def mount_web(app: Any) -> None:
    """掛 `/ui` 靜態檔與 `/` 轉址;`app` 為 FastAPI 實例(只用到 `mount` 與 `add_api_route`)。"""
    root = WEB_ROOT
    static = StaticFiles(directory=root, html=True, follow_symlink=False)
    app.add_api_route("/", _root_redirect, methods=["GET", "HEAD"], include_in_schema=False)
    app.mount(MOUNT_PATH, StaticFilter(static, root), name="ui")

"""`mount_web` 與 `SecurityHeadersMiddleware`(CHG-20261103-01 T3;決策 6、7,決策審議修訂 AM-2)。

L1(真的 uvicorn,`start_live_gateway`;靜態檔根目錄換成暫存複本,才能放 `.DS_Store`、`x.py` 與
指向外部的 symlink 而不碰原始碼樹):
- `GET /`、`HEAD /` → 302 `Location: /ui/`,`/` 不在 OpenAPI;
- `/`、`/ui`(307)、`/ui/nope.html`(404)、`POST /ui/`(405)、`/ui/assets`(404)、304、`HEAD /ui/`
  與 10 個 HTML 的 200 都帶決策 7 (a) 的全部標頭,同名標頭各只出現一次;
- `/api/ledger`、`/api/ledger/{id}/payload`、`/api/nope`(404)帶 `no-store` 與 `nosniff`;
- `/v1/models` 與 `/mcp/all` 不帶 CSP;
- `/ui/assets/js/api.js` 的 Content-Type 為 JavaScript;
- `/ui/.DS_Store`、`/ui/x.py`、指向外部的 symlink → 404;
- raw path 穿越(`/ui/../app.py`、`/ui/%2e%2e/`、`/ui/assets/`)→ 404 且內容不是檔案原文;
- 錯誤 Host → 403。
"""
from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

import spillway.web as web
from spillway.api.http_headers import API_HEADERS, UI_HEADERS
from spillway.app import create_app
from tests.helpers.live_gateway import LiveGateway, start_live_gateway

PAGES = ["index.html", "members.html", "groups.html", "routes.html", "route.html", "mcp.html",
         "ledger.html", "settings.html", "wizard.html", "api-reference.html"]
SECRET_TEXT = "TOP-SECRET-OUTSIDE-FILE"

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
       "require-trusted-types-for 'script'; trusted-types 'none'")


@pytest.fixture(scope="module")
def static_copy(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """原始 `WEB_ROOT` 的暫存複本,加入應被拒絕的檔案。"""
    base = tmp_path_factory.mktemp("web")
    root = base / "static"
    shutil.copytree(web.WEB_ROOT, root)
    (root / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    (root / "x.py").write_text("print('x')\n", encoding="utf-8")
    hidden = root / ".git"
    hidden.mkdir()
    (hidden / "config.html").write_text("<p>hidden</p>", encoding="utf-8")
    outside = base / "outside.html"
    outside.write_text(SECRET_TEXT, encoding="utf-8")
    (root / "evil.html").symlink_to(outside)
    return root


@pytest.fixture(scope="module")
def gw(static_copy: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveGateway]:
    data = tmp_path_factory.mktemp("data")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(web, "WEB_ROOT", static_copy)
        gateway = start_live_gateway(data / "spillway.db")
    try:
        yield gateway
    finally:
        gateway.stop()


@pytest.fixture
def client(gw: LiveGateway) -> Iterator[httpx.Client]:
    with gw.http(follow_redirects=False) as c:
        yield c


def _assert_ui_headers(resp: httpx.Response) -> None:
    for name, value in UI_HEADERS:
        values = resp.headers.get_list(name)
        assert values == [value], (resp.request.url, name, values)
    assert resp.headers["content-security-policy"] == CSP


def _assert_api_headers(resp: httpx.Response) -> None:
    for name, value in API_HEADERS:
        assert resp.headers.get_list(name) == [value], (resp.request.url, name)
    assert "content-security-policy" not in resp.headers


# ---- `/` 轉址 ---------------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_root_redirects_302_with_headers(client: httpx.Client, method: str) -> None:
    resp = client.request(method, "/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/ui/"
    _assert_ui_headers(resp)


def test_root_not_in_openapi(tmp_path: Path) -> None:
    app = create_app(db_path=tmp_path / "s.db")
    assert "/" not in app.openapi()["paths"]
    assert all(not p.startswith("/ui") for p in app.openapi()["paths"])


# ---- `/ui` 的狀態碼與標頭 ----------------------------------------------------------------------

def test_ui_without_slash_307(client: httpx.Client) -> None:
    resp = client.get("/ui")
    assert resp.status_code == 307
    assert resp.headers["location"].endswith("/ui/")
    _assert_ui_headers(resp)


@pytest.mark.parametrize("path", ["/ui/nope.html", "/ui/assets"])
def test_ui_404_with_headers(client: httpx.Client, path: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    _assert_ui_headers(resp)


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_ui_non_get_405(client: httpx.Client, method: str) -> None:
    resp = client.request(method, "/ui/")
    assert resp.status_code == 405
    _assert_ui_headers(resp)


def test_ui_head_and_304(client: httpx.Client) -> None:
    head = client.head("/ui/")
    assert head.status_code == 200
    _assert_ui_headers(head)
    first = client.get("/ui/")
    etag = first.headers["etag"]
    resp = client.get("/ui/", headers={"If-None-Match": etag})
    assert resp.status_code == 304
    _assert_ui_headers(resp)


@pytest.mark.parametrize("page", PAGES)
def test_pages_200_with_headers(client: httpx.Client, page: str) -> None:
    resp = client.get(f"/ui/{page}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    _assert_ui_headers(resp)


def test_index_served_at_ui_root(client: httpx.Client) -> None:
    root = client.get("/ui/")
    assert root.status_code == 200
    assert root.text == client.get("/ui/index.html").text


@pytest.mark.parametrize("asset", ["assets/app.css", "assets/js/api.js", "assets/js/dom.js",
                                   "assets/js/ui.js", "assets/js/strings.js"])
def test_assets_served(client: httpx.Client, asset: str) -> None:
    resp = client.get(f"/ui/{asset}")
    assert resp.status_code == 200
    _assert_ui_headers(resp)
    if asset.endswith(".js"):
        assert "javascript" in resp.headers["content-type"]
    else:
        assert resp.headers["content-type"].startswith("text/css")


# ---- `/api` 一律 no-store,其他路徑不碰 ------------------------------------------------------------

@pytest.mark.parametrize("path,status", [("/api/ledger", 200), ("/api/ledger/999999/payload", 404),
                                         ("/api/nope", 404), ("/api/settings", 200)])
def test_api_no_store(client: httpx.Client, path: str, status: int) -> None:
    resp = client.get(path)
    assert resp.status_code == status
    _assert_api_headers(resp)


def test_api_405_no_store(client: httpx.Client) -> None:
    resp = client.request("PATCH", "/api/settings")
    assert resp.status_code == 405
    _assert_api_headers(resp)


@pytest.mark.parametrize("path", ["/v1/models", "/mcp/all"])
def test_other_paths_untouched(client: httpx.Client, path: str) -> None:
    resp = client.get(path)
    assert "content-security-policy" not in resp.headers
    assert "cross-origin-opener-policy" not in resp.headers
    assert resp.headers.get("cache-control") != "no-store"


# ---- 檔案過濾 ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/ui/.DS_Store", "/ui/x.py", "/ui/evil.html", "/ui/.git/config.html",
                                  "/ui/assets/", "/ui/assets/js/", "/ui/INDEX.HTML"])
def test_filtered_paths_404(client: httpx.Client, path: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 404
    assert SECRET_TEXT not in resp.text
    assert "Bud1" not in resp.text
    _assert_ui_headers(resp)


@pytest.mark.parametrize("raw", [b"/ui/../app.py", b"/ui/%2e%2e/", b"/ui/%2e%2e/%2e%2e/app.py",
                                 b"/ui/assets/../../app.py", b"/ui/..%2fapp.py", b"/ui/assets/",
                                 b"/ui/..%5capp.py", b"/ui/./index.html"])
def test_traversal_raw_path_404(client: httpx.Client, raw: bytes) -> None:
    resp = client.get("/ui/", extensions={"target": raw})
    assert resp.status_code == 404, raw
    assert "def create_app" not in resp.text
    assert "<nav" not in resp.text
    assert SECRET_TEXT not in resp.text


def test_wrong_host_403(client: httpx.Client) -> None:
    resp = client.get("/ui/", headers={"Host": "evil.example"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "host_not_allowed"


def test_wrong_origin_403(gw: LiveGateway, client: httpx.Client) -> None:
    resp = client.get("/ui/", headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403
    ok = client.get("/ui/", headers={"Origin": gw.base_url})
    assert ok.status_code == 200

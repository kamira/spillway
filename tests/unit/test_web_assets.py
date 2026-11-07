"""前端靜態檔的機械檢查(CHG-20261103-01 T3;決策 5–9、20,決策審議修訂 AM-12、AM-16)。

T4–T8 新增的檔案自動納入:
- `web/static/**/*.js`:決策 8 的禁用字樣命中 0;共用模組不呼叫 `console`;不含非本機網址。
- `web/static/**/*.html`:無 `<script>` 內文、無 inline 事件屬性、無 `style` 屬性與 `<style>`、無
  `http://`/`https://`;每頁只以一個 `<script type="module" src="assets/js/<page>.js">` 載入。
- 10 個 HTML 的導覽區塊(8 個連結的文字與 href,文字依決策 20)逐字相同,各頁 `aria-current` 指向自己
  (精靈指向「總覽」、route 編輯頁指向「Route」)。
- `src/spillway/web/**/*.py` 只 import 標準庫與 `starlette`;import `spillway.web` 之後 `sys.modules`
  不含 `spillway.api.admin`、`spillway.store`、`spillway.routing`(AM-16)。
- `web/static` 下每個檔案都符合 pyproject 的 package-data glob,且副檔名只有 html/css/js。
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PKG = REPO / "src" / "spillway"
WEB = PKG / "web"
STATIC = WEB / "static"

NAV = [("總覽", "/ui/"), ("成員", "/ui/members.html"), ("群組與 Tag", "/ui/groups.html"),
       ("Route", "/ui/routes.html"), ("MCP 設定", "/ui/mcp.html"), ("帳本", "/ui/ledger.html"),
       ("設定", "/ui/settings.html"), ("API 參考", "/ui/api-reference.html")]

# 頁面 → (h1 標題, aria-current 指向的導覽文字, 頁面 script)
PAGES = {
    "index.html": ("總覽", "總覽", "overview.js"),
    "members.html": ("成員", "成員", "members.js"),
    "groups.html": ("群組與 Tag", "群組與 Tag", "groups.js"),
    "routes.html": ("Route", "Route", "routes.js"),
    "route.html": ("編輯 Route", "Route", "route-editor.js"),
    "mcp.html": ("MCP 設定", "MCP 設定", "mcp.js"),
    "ledger.html": ("帳本", "帳本", "ledger.js"),
    "settings.html": ("設定", "設定", "settings.js"),
    "wizard.html": ("首次設定", "總覽", "wizard.js"),
    "api-reference.html": ("API 參考", "API 參考", "api-reference.js"),
}

# 決策 8(含 AM-12)的禁用字樣
BANNED_JS = {
    "innerHTML": r"innerHTML",
    "outerHTML": r"outerHTML",
    "insertAdjacentHTML": r"insertAdjacentHTML",
    "document.write": r"document\s*\.\s*write",
    "eval": r"\beval\b",
    "new Function": r"\bnew\s+Function\b",
    "Function()": r"(?<![\w.])Function\s*\(",
    "setTimeout/setInterval 字串": r"\bset(?:Timeout|Interval)\s*\(\s*[\"'`]",
    ".href =": r"\.\s*href\s*=(?!=)",
    ".src =": r"\.\s*src\s*=(?!=)",
    ".action =": r"\.\s*action\s*=(?!=)",
    ".formAction =": r"\.\s*formAction\s*=(?!=)",
    ".srcdoc": r"\.\s*srcdoc\b",
    # 宣告名為 location 的區域變數不算(仍建議不要用這個名稱)
    "location =": r"(?<!let )(?<!const )(?<!var )\blocation\s*=(?!=)",
    "location.href =": r"\blocation\s*\.\s*href\s*=(?!=)",
    "location.assign": r"\blocation\s*\.\s*assign\b",
    "location.replace": r"\blocation\s*\.\s*replace\b",
    "window.open(": r"\bwindow\s*\.\s*open\s*\(",
    "createContextualFragment": r"createContextualFragment",
    "DOMParser": r"DOMParser",
    "setHTMLUnsafe": r"setHTMLUnsafe",
    "execCommand": r"execCommand",
    "import(非字面)": r"\bimport\s*\(\s*(?![\"'][^\"'`$]*[\"']\s*\))",
}
SHARED_JS = ("api.js", "dom.js", "ui.js", "strings.js")
EXTERNAL_URL = re.compile(r"https?://(?!127\.0\.0\.1[:/]|localhost[:/])", re.I)


def _files(suffix: str) -> list[Path]:
    return sorted(p for p in STATIC.rglob(f"*{suffix}") if p.is_file())


def _ids(paths: list[Path]) -> list[str]:
    return [str(p.relative_to(STATIC)) for p in paths]


# ---- JS 靜態掃描 -----------------------------------------------------------------------------------

def test_static_tree_has_expected_files() -> None:
    assert sorted(p.name for p in STATIC.glob("*.html")) == sorted(PAGES)
    for name in SHARED_JS:
        assert (STATIC / "assets" / "js" / name).is_file()
    assert (STATIC / "assets" / "app.css").is_file()


@pytest.mark.parametrize("path", _files(".js"), ids=_ids(_files(".js")))
def test_js_banned_patterns(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    hits = [(label, m.group(0)) for label, rx in BANNED_JS.items() for m in re.finditer(rx, text)]
    assert hits == []
    assert EXTERNAL_URL.findall(text) == []


@pytest.mark.parametrize("name", SHARED_JS)
def test_shared_js_no_console(name: str) -> None:
    text = (STATIC / "assets" / "js" / name).read_text(encoding="utf-8")
    assert "console." not in text


@pytest.mark.parametrize("label,sample", [
    ("innerHTML", "el.innerHTML = x"), ("eval", "eval(s)"), ("new Function", "new Function('a')"),
    ("setTimeout/setInterval 字串", "setTimeout('x()', 1)"), (".href =", "a.href = u"),
    (".src =", "img.src=u"), ("location.href =", "location.href = '/ui/'"),
    ("location.assign", "location.assign(u)"), ("window.open(", "window.open(u)"),
    ("import(非字面)", "import(name)"), ("import(非字面)", "import(`./${x}.js`)"),
    ("DOMParser", "new DOMParser()"), (".srcdoc", "f.srcdoc = s"), ("location =", "location = u"),
    ("location =", "window.location = u"),
])
def test_banned_patterns_detect_samples(label: str, sample: str) -> None:
    assert re.search(BANNED_JS[label], sample)


@pytest.mark.parametrize("sample", ["a.href === b", "import('./x.js')", 'import("./ui.js")',
                                    "x.evaluate()", "setTimeout(fn, 300)", "el.dataset.src",
                                    "let location = 1", "const location = x"])
def test_banned_patterns_allow_samples(sample: str) -> None:
    assert [k for k, rx in BANNED_JS.items() if re.search(rx, sample)] == []


@pytest.mark.parametrize("path", _files(".css"), ids=_ids(_files(".css")))
def test_css_no_external(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert EXTERNAL_URL.findall(text) == []
    assert "@import" not in text
    assert "url(" not in text


# ---- HTML 掃描 ------------------------------------------------------------------------------------

class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[dict[str, str | None]] = []
        self.script_text: list[str] = []
        self.styles = 0
        self.attrs: list[tuple[str, str, str | None]] = []
        self.nav_links: list[tuple[str, str | None, str | None]] = []
        self.navs = 0
        self.h1: list[str] = []
        self.mains = 0
        self.lang: str | None = None
        self._in = {"nav": 0, "script": 0, "a": 0, "h1": 0, "main": 0}
        self._a: dict[str, str | None] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = dict(attrs)
        for k, v in attrs:
            self.attrs.append((tag, k, v))
        if tag == "html":
            self.lang = d.get("lang")
        if tag == "script":
            self.scripts.append(d)
            self._in["script"] += 1
        if tag == "style":
            self.styles += 1
        if tag == "nav":
            self.navs += 1
            self._in["nav"] += 1
        if tag == "main":
            self.mains += 1
            self._in["main"] += 1
        if tag == "a" and self._in["nav"]:
            self._a = d
            self._text = []
        if tag == "h1" and self._in["main"]:
            self._in["h1"] += 1
            self._text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in["script"] -= 1
        if tag == "nav":
            self._in["nav"] -= 1
        if tag == "main":
            self._in["main"] -= 1
        if tag == "a" and self._a is not None:
            self.nav_links.append(("".join(self._text).strip(), self._a.get("href"),
                                   self._a.get("aria-current")))
            self._a = None
        if tag == "h1" and self._in["h1"]:
            self._in["h1"] -= 1
            self.h1.append("".join(self._text).strip())

    def handle_data(self, data: str) -> None:
        if self._in["script"] and data.strip():
            self.script_text.append(data)
        if self._a is not None or self._in["h1"]:
            self._text.append(data)


def _parse(name: str) -> tuple[str, _Page]:
    text = (STATIC / name).read_text(encoding="utf-8")
    page = _Page()
    page.feed(text)
    page.close()
    return text, page


@pytest.mark.parametrize("name", sorted(PAGES))
def test_html_no_inline_code_or_external(name: str) -> None:
    text, page = _parse(name)
    assert page.script_text == []
    assert page.styles == 0
    assert [a for a in page.attrs if a[1].lower().startswith("on")] == []
    assert [a for a in page.attrs if a[1].lower() == "style"] == []
    assert not re.search(r"https?://", text, re.I)
    assert not re.search(r"\son\w+\s*=", text, re.I)
    assert not re.search(r"\sstyle\s*=", text, re.I)
    assert page.lang == "zh-Hant"


@pytest.mark.parametrize("name", sorted(PAGES))
def test_html_single_module_script(name: str) -> None:
    _, page = _parse(name)
    assert page.scripts == [{"type": "module", "src": f"assets/js/{PAGES[name][2]}"}]


@pytest.mark.parametrize("name", sorted(PAGES))
def test_html_main_has_title(name: str) -> None:
    _, page = _parse(name)
    assert page.mains == 1
    assert page.h1 == [PAGES[name][0]]


def test_nav_identical_across_pages() -> None:
    blocks = {}
    for name in PAGES:
        text, page = _parse(name)
        assert page.navs == 1
        assert [(t, h) for t, h, _ in page.nav_links] == NAV, name
        # 導覽區塊去掉 aria-current 後逐字相同
        block = re.search(r"<nav\b.*?</nav>", text, re.S)
        assert block is not None
        blocks[name] = re.sub(r'\s+aria-current="page"', "", block.group(0))
    assert len(set(blocks.values())) == 1


@pytest.mark.parametrize("name", sorted(PAGES))
def test_nav_aria_current_points_to_self(name: str) -> None:
    _, page = _parse(name)
    current = [t for t, _, cur in page.nav_links if cur is not None]
    assert current == [PAGES[name][1]]
    assert [cur for _, _, cur in page.nav_links if cur is not None] == ["page"]


# ---- 檔案過濾函式 ---------------------------------------------------------------------------------

@pytest.mark.parametrize("path,dirs,expected", [
    ("/ui/", {""}, True), ("/ui/assets/", {"", "assets"}, True), ("/ui/index.html", set(), True),
    ("/ui/assets/js/api.js", set(), True),
    ("/ui/x.py/", {""}, False), ("/ui/notes.md/", {""}, False), ("/ui/index.html/", {""}, False),
    ("/ui/x.py", {""}, False), ("/ui/assets", {"assets"}, False), ("/ui/.git/", {".git"}, False),
    ("/ui/../app.py", set(), False), ("/ui/a\\b.html", set(), False), ("/ui/", None, False),
])
def test_is_allowed_path(path: str, dirs: set[str] | None, expected: bool) -> None:
    from spillway.web import is_allowed_path
    is_dir = None if dirs is None else (lambda rel: rel in dirs)
    assert is_allowed_path(path, is_dir) is expected


def test_static_filter_dir_check_uses_root(tmp_path: Path) -> None:
    from spillway.web import StaticFilter
    (tmp_path / "sub").mkdir()
    (tmp_path / "x.py").write_text("x", encoding="utf-8")
    f = StaticFilter(lambda *a: None, tmp_path)
    assert f._is_dir("") and f._is_dir("sub")
    assert not f._is_dir("x.py") and not f._is_dir("nope")


# ---- 架構守門(AM-16)-----------------------------------------------------------------------------

def _py_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names.add(".")
            elif node.module:
                names.add(node.module.split(".")[0])
    return names


def test_web_py_imports_only_stdlib_and_starlette() -> None:
    pys = sorted(WEB.rglob("*.py"))
    assert pys
    allowed = set(sys.stdlib_module_names) | {"starlette", "__future__"}
    for py in pys:
        bad = _py_imports(py) - allowed
        assert bad == set(), (py, bad)


def test_import_web_does_not_load_backend() -> None:
    code = ("import sys, spillway.web; "
            "bad = [m for m in sys.modules if m.startswith(('spillway.api.admin', 'spillway.store', "
            "'spillway.routing'))]; print(bad); sys.exit(1 if bad else 0)")
    proc = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                          env={"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin"}, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_static_files_match_package_data() -> None:
    conf = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = conf["tool"]["setuptools"]["package-data"]["spillway"]
    matched: set[Path] = set()
    for pat in patterns:
        matched.update(p for p in PKG.glob(pat) if p.is_file())
    files = {p for p in STATIC.rglob("*") if p.is_file()}
    assert files
    assert files - matched == set()
    assert {p.suffix for p in files} <= {".html", ".css", ".js"}


def test_playwright_dev_dependency_pinned() -> None:
    conf = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert "playwright==1.64.1" in conf["project"]["optional-dependencies"]["dev"]

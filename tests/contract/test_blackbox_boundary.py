"""黑箱邊界的靜態守門(CHG-20261107-03 T2;決策 9;AM-8、AM-14;契約清單「黑箱邊界」)。

範圍:`tests/blackbox/guard.converted_roots()`——`tests/blackbox` 加上 `tests/blackbox/converted_roots.txt`
列出的目錄(wave 3 的 T3、T4、T5 轉換完成時各加一行自己的目錄,不必改本檔)。範圍內每個 `.py` 檔為起點,
另加根 `tests/conftest.py`(pytest 對每個測試都會載入它;它與 unit、integration 共用,只檢查載入時就會執行的
import——函式內的延遲 import 與 `TYPE_CHECKING` 區塊不算,決策 9 ⑤)。

規則
1. 遞移閉包:從起點沿 `tests.*` 的 import(含函式內的延遲 import、`TYPE_CHECKING` 區塊、相對 import、
   `pytest_plugins`、`importlib.import_module("…")`/`__import__("…")`)展開,閉包內任何檔案都不得 import
   `spillway` 或 `litellm`。
2. 允許的 `tests.*`:範圍內的檔案(與它們再引用的允許模組)只可 import `tests.blackbox.*`、
   `tests.fakes.api_upstream.*`、`tests.fakes.cli_upstream.*`、範圍內的模組,以及套件本身(`tests`、
   `tests.fakes`)。`tests.fakes.{claude_cli,codex_cli}`、`tests.helpers`、`tests.integration`、`tests.contract`
   等一律違規(假 CLI 只能以子行程執行)。根 conftest 只檢查規則 1。
3. 字面路徑(範圍內的檔案):字串常數含 `src/spillway`,或以 `/ "src"`、`os.path.join(…, "src", …)` 組出路徑;
   以 `spillway`、`litellm` 為參數呼叫 `import_module`、`__import__`、`find_spec`、`files`。
   例外列在 `tests/blackbox/boundary_allowlist.toml`(檔案、字面值或 regex、理由)。組出的路徑以整個運算式
   (`ast.unparse`)作為字面值:`/` 鏈取最外層那一條(鏈中含 `"src"` 常數即判),`os.path.join(…)`、
   `Path("src", "spillway", …)` 取整個呼叫——允許一條路徑不會放行同檔的其他 `/ "src"` 路徑(終審 F1)。
4. 字樣(AM-14,範圍內的檔案):字串常數(docstring 除外)不得含 `uvicorn` 或 `INFO:`;不得 import `uvicorn`。
5.(施工審議 T2 F3)範圍內的檔案:同一呼叫的字串參數依序含 `"src"`、`"spillway"`(`joinpath`、`Path`…)判
   `src-path`;改動 `sys.path`(`insert`/`append`/`extend`/賦值/`+=`)判 `sys-path`;字串常數含
   `import spillway`/`from litellm` 之類的程式碼,或 `-m spillway`(含 `["-m", "spillway"]` 相鄰元素)判
   `inproc-string`(行程內或繞過 `SPILLWAY_CMD`;harness 的預設命令列在允許清單)。嚴格檔案中的裸頂層 import
   對應到 `tests/**` 的模組名(stdlib 與已安裝套件除外)判 `tests-module`(經 `sys.path` 改動繞過閉包)。
   以變數或執行期組出的名稱靜態攔不到,由執行期守門(`tests/blackbox/plugin.py` 的阻擋器)處理。

L1 以暫存目錄中的違規樣本(直接 import、經 `tests.helpers` 間接 import、字面 `src/spillway/` 路徑、`uvicorn`
字樣等)驗證守門會失敗,並以合規樣本驗證會通過;最後對本 repo 的實際範圍執行。
"""
from __future__ import annotations

import ast
import importlib.util
import re
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.blackbox.guard import CONVERTED_ROOTS_FILE, converted_roots

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_FILE = REPO_ROOT / "tests" / "blackbox" / "boundary_allowlist.toml"
FORBIDDEN_TOP = ("spillway", "litellm")
ALLOWED_TESTS_PREFIXES = ("tests.blackbox", "tests.fakes.api_upstream", "tests.fakes.cli_upstream")
PACKAGE_MODULES = ("tests", "tests.fakes")
IMPORT_CALLS = {"import_module", "__import__"}
# `importlib` / `runpy` / `pkgutil` / `importlib.metadata` 中以模組或發行名定位的呼叫(F3 b)
LOCATOR_CALLS = IMPORT_CALLS | {"find_spec", "files", "run_module", "resolve_name", "distribution", "metadata",
                                "version"}
SYS_PATH_METHODS = {"insert", "append", "extend"}
_INPROC_STR = re.compile(r"(^|[\s;])(import|from)\s+(spillway|litellm)\b|-m\s*['\"]?\s*,?\s*['\"]?spillway\b")
_FORBIDDEN_ARG = re.compile(r"^(spillway|litellm)(\.|$)")
_SRC_PATH = re.compile(r"src[/\\]+spillway")
_WORDS = ("uvicorn", "INFO:")


@dataclass(frozen=True)
class Violation:
    file: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.file}: [{self.rule}] {self.detail}"


def load_allowlist(path: Path = ALLOWLIST_FILE) -> set[tuple[str, str]]:
    """`(file, literal)`:`literal` 為完全相同的字面值;項目改用 `pattern` 時記成 `(file, "re:" + pattern)`,
    字面值符合該 regex(`re.search`)即豁免。"""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out = set()
    for e in data.get("allow", []):
        assert e.get("reason"), f"允許清單的項目必須寫明理由:{e}"
        assert ("literal" in e) != ("pattern" in e), f"允許清單的項目要剛好一個 literal 或 pattern:{e}"
        out.add((e["file"], e["literal"] if "literal" in e else "re:" + e["pattern"]))
    return out


def _allowed(allow: set[tuple[str, str]], rel: str, literal: str) -> bool:
    for f, lit in allow:
        if f != rel:
            continue
        if lit == literal or (lit.startswith("re:") and re.search(lit[3:], literal)):
            return True
    return False


# ---------------------------------------------------------------------------- 解析


def _module_of(repo: Path, path: Path) -> str:
    rel = path.relative_to(repo).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _load_time_nodes(tree: ast.Module) -> list[ast.AST]:
    """模組載入時就會執行的節點:略過函式本體與 `if TYPE_CHECKING:` 區塊(根 conftest 的延遲 import 用)。"""
    out: list[ast.AST] = []
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
            stack.extend(node.orelse)
            continue
        out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


def imports_of(repo: Path, path: Path, tree: ast.AST, *, load_time_only: bool = False) -> list[str]:
    """檔案引用的模組名稱(絕對名稱;`from a import b` 列出 `a`,`a.b` 是 repo 內的子模組時也列出)。"""
    mod = _module_of(repo, path)
    pkg = mod if path.name == "__init__.py" else mod.rpartition(".")[0]
    names: list[str] = []
    nodes = _load_time_nodes(tree) if load_time_only else list(ast.walk(tree))   # type: ignore[arg-type]
    for node in nodes:
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = pkg.split(".") if pkg else []
                base_parts = base_parts[: len(base_parts) - (node.level - 1)] if node.level > 1 else base_parts
                base = ".".join([*base_parts, *([node.module] if node.module else [])])
            else:
                base = node.module or ""
            if base:
                names.append(base)
            for a in node.names:   # `from tests.x import y`:y 是子模組時才算一個模組
                full = f"{base}.{a.name}" if base else a.name
                if a.name != "*" and (not base or _module_file(repo, full) is not None):
                    names.append(full)
        elif isinstance(node, ast.Call) and _call_name(node.func) in IMPORT_CALLS and node.args \
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            names.append(node.args[0].value)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "pytest_plugins" for t in targets) and node.value is not None:
                for c in ast.walk(node.value):
                    if isinstance(c, ast.Constant) and isinstance(c.value, str):
                        names.append(c.value)
    return names


def _module_file(repo: Path, name: str) -> Path | None:
    base = repo.joinpath(*name.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def _in_roots(rel: str, roots: Iterable[str]) -> bool:
    return any(rel == r or rel.startswith(r + "/") for r in roots)


def _is_sys_path(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "path" and isinstance(node.value, ast.Name) \
        and node.value.id == "sys"


def _is_div(node: ast.AST | None) -> bool:
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)


def _div_constants(node: ast.expr) -> list[object]:
    """`/` 鏈(左右兩側遞迴)中的常數值。"""
    if _is_div(node):
        return [*_div_constants(node.left), *_div_constants(node.right)]
    return [node.value] if isinstance(node, ast.Constant) else []


def _literal_violations(rel: str, tree: ast.AST, allow: set[tuple[str, str]]) -> list[Violation]:
    out: list[Violation] = []
    docs = _docstring_nodes(tree)
    parents = {id(c): n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}

    def flag(rule: str, literal: str) -> None:
        if not _allowed(allow, rel, literal):
            out.append(Violation(rel, rule, literal))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            if _SRC_PATH.search(node.value):
                flag("src-path", node.value)
            for w in _WORDS:
                if w in node.value:
                    flag("word", node.value)
            if _INPROC_STR.search(node.value):
                flag("inproc-string", node.value)
        elif isinstance(node, (ast.List, ast.Tuple)):
            elts = [e.value if isinstance(e, ast.Constant) and isinstance(e.value, str) else None for e in node.elts]
            for a, b in zip(elts, elts[1:]):
                if a == "-m" and b is not None and re.match(r"spillway(\.|$)", b):
                    flag("inproc-string", f"-m {b}")
        elif isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(_is_sys_path(t) for t in targets):
                flag("sys-path", "sys.path")
        elif _is_div(node):
            # 只判最外層的 `/` 鏈,字面值是整條運算式(終審 F1:允許清單分得出組出來的是哪一條路徑)
            if not _is_div(parents.get(id(node))) and "src" in _div_constants(node):
                flag("src-path", ast.unparse(node))
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            args = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if name == "join" and "src" in args:
                flag("src-path", ast.unparse(node))
            elif "src" in args and "spillway" in args[args.index("src") + 1:]:   # joinpath、Path("src", "spillway")
                flag("src-path", ast.unparse(node))
            if isinstance(node.func, ast.Attribute) and node.func.attr in SYS_PATH_METHODS \
                    and _is_sys_path(node.func.value):
                flag("sys-path", "sys.path")
            if name in LOCATOR_CALLS and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str) and _FORBIDDEN_ARG.match(node.args[0].value):
                flag("locate", node.args[0].value)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            if any(m.split(".")[0] == "uvicorn" for m in mods):
                flag("word", "import uvicorn")
    return out


# ---------------------------------------------------------------------------- 檢查


def _tests_module_names(repo: Path) -> set[str]:
    """`tests/**` 下的模組與套件名(檔名不含 .py、含 `__init__.py` 的目錄名);裸 import 用(F3 d)。"""
    root = repo / "tests"
    names = {p.stem for p in root.rglob("*.py") if p.name != "__init__.py" and "__pycache__" not in p.parts}
    names |= {p.parent.name for p in root.rglob("__init__.py") if p.parent != root}
    return names


def _installed(top: str) -> bool:
    """stdlib 或已安裝的套件(不在 `tests/` 底下)。"""
    if top in sys.stdlib_module_names:
        return True
    try:
        spec = importlib.util.find_spec(top)
    except (ImportError, ValueError):
        return False
    return spec is not None and not (spec.origin or "").startswith(str(REPO_ROOT / "tests"))


def check_boundary(repo: Path, roots: list[str], allow: set[tuple[str, str]] | None = None) -> list[Violation]:
    allow = load_allowlist() if allow is None else allow
    roots = [r.rstrip("/") for r in roots]
    starts: list[Path] = []
    for r in roots:
        d = repo / r
        if d.is_dir():
            starts += sorted(p for p in d.rglob("*.py") if "__pycache__" not in p.parts)
    implicit = [p for p in (repo / "tests" / "conftest.py",) if p.is_file()]

    tests_names = _tests_module_names(repo)
    violations: list[Violation] = []
    seen: dict[Path, list[str]] = {}          # 檔案 → 從起點到它的引用鏈
    strict: set[Path] = set()                 # 套用規則 2 的檔案
    queue: list[tuple[Path, list[str], bool]] = [(p, [p.relative_to(repo).as_posix()], True) for p in starts]
    queue += [(p, [p.relative_to(repo).as_posix()], False) for p in implicit]
    while queue:
        path, chain, is_strict = queue.pop(0)
        if path in seen and (path in strict or not is_strict):
            continue
        seen[path] = chain
        if is_strict:
            strict.add(path)
        rel = path.relative_to(repo).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        if _in_roots(rel, roots):
            violations += _literal_violations(rel, tree, allow)
        names = imports_of(repo, path, tree, load_time_only=path in implicit)
        if is_strict:
            for name in names:
                top = name.split(".")[0]
                if top != "tests" and top in tests_names and not _installed(top):
                    violations.append(Violation(chain[0], "tests-module",
                                                f"{' → '.join(chain)} imports {name}(裸 import 對應到 tests/ 下的檔案)"))
        for name in names:
            top = name.split(".")[0]
            if top in FORBIDDEN_TOP:
                violations.append(Violation(chain[0], "closure", f"{' → '.join(chain)} imports {name}"))
                continue
            if top != "tests":
                continue
            target_rel = name.replace(".", "/")
            allowed = (name in PACKAGE_MODULES or name.startswith(tuple(p + "." for p in ALLOWED_TESTS_PREFIXES))
                       or name in ALLOWED_TESTS_PREFIXES or _in_roots(target_rel, roots))
            if is_strict and not allowed:
                violations.append(Violation(chain[0], "tests-module", f"{' → '.join(chain)} imports {name}"))
            parts = name.split(".")
            for i in range(1, len(parts) + 1):   # 套件本身也會被執行(`tests.a.b` 先載入 `tests.a`)
                f = _module_file(repo, ".".join(parts[:i]))
                if f is not None and f != path:
                    queue.append((f, [*chain, f.relative_to(repo).as_posix()], is_strict and allowed))
    # 同一違規可能經多條鏈出現:只保留第一條
    unique: dict[tuple[str, str, str], Violation] = {}
    for v in violations:
        key = (v.rule, v.detail.rsplit(" imports ", 1)[-1], v.file) if v.rule in ("closure", "tests-module") \
            else (v.rule, v.detail, v.file)
        unique.setdefault(key, v)
    return list(unique.values())


# ============================================================================ 樣本(L1)


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    base = {"tests/__init__.py": "", "tests/conftest.py": "", "tests/blackbox/__init__.py": "",
            "tests/fakes/__init__.py": "", "tests/fakes/api_upstream/__init__.py": "import uvicorn\n",
            "tests/fakes/cli_upstream/__init__.py": "", "tests/sample/__init__.py": ""}
    for rel, text in {**base, **files}.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return repo


def _rules(vs: list[Violation]) -> list[str]:
    return sorted(v.rule for v in vs)


COMPLIANT = '''"""說明文字可以提到 uvicorn 與 INFO: 字樣,也可以提到 src/spillway/。"""
import json
import sys
from tests.blackbox import harness
from tests.fakes.api_upstream import server
from tests.fakes.cli_upstream import install
from . import helper
CMD = [sys.executable, "-m", "pytest"]


def f():
    """docstring 也不算:uvicorn"""
    from tests.sample.helper import x
    return json.dumps(CMD)
'''


@pytest.mark.parametrize("name,files,rules", [
    ("compliant", {"tests/sample/test_ok.py": COMPLIANT, "tests/sample/helper.py": "x = 1\n",
                   "tests/blackbox/harness.py": "import httpx\n"}, []),
    ("direct import", {"tests/sample/test_a.py": "import spillway.app\n"}, ["closure"]),
    ("from import", {"tests/sample/test_a.py": "from spillway import cli\n"}, ["closure"]),
    ("lazy import in function", {"tests/sample/test_a.py": "def f():\n    from spillway.clock import FakeClock\n"},
     ["closure"]),
    ("type checking import", {"tests/sample/test_a.py":
                              "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import spillway\n"}, ["closure"]),
    ("import litellm", {"tests/sample/test_a.py": "import litellm\n"}, ["closure"]),
    ("indirect via tests.helpers", {"tests/sample/test_a.py": "from tests.helpers.ui import x\n",
                                    "tests/helpers/__init__.py": "from spillway.domain import Deployment\n",
                                    "tests/helpers/ui.py": "x = 1\n"}, ["closure", "tests-module"]),
    ("indirect via allowed module", {"tests/sample/test_a.py": "from tests.blackbox.bad import y\n",
                                     "tests/blackbox/bad.py": "import spillway.store.repo\ny = 1\n"},
     ["closure"]),
    ("relative import chain", {"tests/sample/test_a.py": "from .inner import z\n",
                               "tests/sample/inner.py": "from spillway import settings\nz = 1\n"},
     ["closure"]),
    ("root conftest", {"tests/conftest.py": "from spillway.clock import FakeClock\n",
                       "tests/sample/test_a.py": "x = 1\n"}, ["closure"]),
    ("root conftest via helper", {"tests/conftest.py": "from tests.helpers import fake_key_env\n",
                                  "tests/helpers/__init__.py": "import spillway.domain\n",
                                  "tests/sample/test_a.py": "x = 1\n"}, ["closure"]),
    ("root conftest lazy import is allowed", {
        "tests/conftest.py": ("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from spillway.clock import X\n"
                              "def clock():\n    from spillway.clock import FakeClock\n    return FakeClock()\n"),
        "tests/sample/test_a.py": "x = 1\n"}, []),
    ("pytest_plugins", {"tests/sample/test_a.py": 'pytest_plugins = ["tests.blackbox.p"]\n',
                        "tests/blackbox/p.py": "import spillway\n"}, ["closure"]),
    ("import_module", {"tests/sample/test_a.py": 'import importlib\nm = importlib.import_module("spillway.app")\n'},
     ["closure", "locate"]),
    ("resources files", {"tests/sample/test_a.py": 'from importlib import resources\nresources.files("spillway.store")\n'},
     ["locate"]),
    ("fake claude cli module", {"tests/sample/test_a.py": "from tests.fakes.claude_cli import X\n",
                                "tests/fakes/claude_cli/__init__.py": "X = 1\n"}, ["tests-module"]),
    ("integration conftest", {"tests/sample/test_a.py": "from tests.integration.conftest import open_spillway\n",
                              "tests/integration/__init__.py": "",
                              "tests/integration/conftest.py": "from spillway.app import create_app\n"},
     ["closure", "tests-module"]),
    ("literal src path", {"tests/sample/test_a.py": 'P = "src/spillway/adapters/x/models.yaml"\n'}, ["src-path"]),
    ("path join with src", {"tests/sample/test_a.py": "from pathlib import Path\nP = Path('.') / 'src'\n"},
     ["src-path"]),
    ("os.path.join with src", {"tests/sample/test_a.py": "import os\nP = os.path.join('a', 'src', 'spillway')\n"},
     ["src-path"]),
    ("uvicorn string", {"tests/sample/test_a.py": 'assert "uvicorn" not in "x"\n'}, ["word"]),
    ("INFO string", {"tests/sample/test_a.py": 'LINE = "INFO:     Started"\n'}, ["word"]),
    ("import uvicorn", {"tests/sample/test_a.py": "import uvicorn\n"}, ["word"]),
    # ---- 施工審議 T2 F3
    ("joinpath src spillway", {"tests/sample/test_a.py":
                               "from pathlib import Path\nP = Path('.').joinpath('src', 'spillway', 'app.py')\n"},
     ["src-path"]),
    ("Path src spillway", {"tests/sample/test_a.py": "from pathlib import Path\nP = Path('src', 'spillway')\n"},
     ["src-path"]),
    ("Path src elsewhere ok", {"tests/sample/test_a.py": "from pathlib import Path\nP = Path('spillway', 'src')\n"},
     []),
    ("runpy run_module", {"tests/sample/test_a.py": "import runpy\nrunpy.run_module('spillway.cli')\n"}, ["locate"]),
    ("pkgutil resolve_name", {"tests/sample/test_a.py": "import pkgutil\npkgutil.resolve_name('spillway.app:x')\n"},
     ["locate"]),
    ("metadata distribution", {"tests/sample/test_a.py":
                               "import importlib.metadata as m\nm.distribution('spillway')\nm.version('spillway')\n"},
     ["locate"]),
    ("metadata of other package ok", {"tests/sample/test_a.py": "import importlib.metadata as m\nm.version('httpx')\n"},
     []),
    ("sys.path insert", {"tests/sample/test_a.py": "import sys\nsys.path.insert(0, 'x')\n"}, ["sys-path"]),
    ("sys.path append", {"tests/sample/test_a.py": "import sys\nsys.path.append('x')\n"}, ["sys-path"]),
    ("sys.path assign", {"tests/sample/test_a.py": "import sys\nsys.path = ['x']\n"}, ["sys-path"]),
    ("sys.path augassign", {"tests/sample/test_a.py": "import sys\nsys.path += ['x']\n"}, ["sys-path"]),
    ("sys.path read ok", {"tests/sample/test_a.py": "import sys\nN = len(sys.path)\n"}, []),
    ("bare import of tests module", {"tests/sample/test_a.py": "import helpers\n",
                                     "tests/helpers/__init__.py": "import spillway\n"}, ["tests-module"]),
    ("bare from-import of tests file", {"tests/sample/test_a.py": "from live_gateway import x\n",
                                        "tests/helpers/live_gateway.py": "x = 1\n"}, ["tests-module"]),
    ("bare stdlib import ok", {"tests/sample/test_a.py": "import json\nimport signal\n",
                               "tests/sample/json.py": "", "tests/sample/signal.py": ""}, []),
    ("subprocess -c import", {"tests/sample/test_a.py":
                              "import subprocess, sys\nsubprocess.run([sys.executable, '-c', 'from spillway.store import db'])\n"},
     ["inproc-string"]),
    ("code string import litellm", {"tests/sample/test_a.py": "CODE = 'x = 1; import litellm'\n"}, ["inproc-string"]),
    ("-m spillway list", {"tests/sample/test_a.py": "import sys\nCMD = [sys.executable, '-m', 'spillway', 'serve']\n"},
     ["inproc-string"]),
    ("-m spillway string", {"tests/sample/test_a.py": "CMD = 'python -m spillway serve'\n"}, ["inproc-string"]),
    ("prose mentioning spillway ok", {"tests/sample/test_a.py": "MSG = 'spillway: 已產生 token;spillway serve 結束'\n"},
     []),
])
def test_boundary_samples(tmp_path, name, files, rules):
    repo = _repo(tmp_path, files)
    got = check_boundary(repo, ["tests/blackbox", "tests/sample"], allow=set())
    assert _rules(got) == sorted(rules), (name, [str(v) for v in got])


def test_fakes_may_import_uvicorn_but_not_spillway(tmp_path):
    """允許的假上游模組本身可以 import uvicorn(它不是轉換後的檔案),但不可 import spillway。"""
    repo = _repo(tmp_path, {"tests/sample/test_a.py": "from tests.fakes.api_upstream import s\n"})
    assert check_boundary(repo, ["tests/sample"], allow=set()) == []
    (repo / "tests/fakes/api_upstream/__init__.py").write_text("import spillway\n", encoding="utf-8")
    assert _rules(check_boundary(repo, ["tests/sample"], allow=set())) == ["closure"]


def test_allowlist_suppresses_only_listed_literal(tmp_path):
    repo = _repo(tmp_path, {"tests/sample/test_a.py": 'import importlib.util\nimportlib.util.find_spec("litellm")\n'
                                                      'P = "src/spillway/x"\n'})
    assert _rules(check_boundary(repo, ["tests/sample"], allow=set())) == ["locate", "src-path"]
    allow = {("tests/sample/test_a.py", "litellm")}
    assert _rules(check_boundary(repo, ["tests/sample"], allow=allow)) == ["src-path"]
    allow = {("tests/sample/test_a.py", "litellm"), ("tests/sample/test_a.py", r"re:^src/spillway/")}
    assert check_boundary(repo, ["tests/sample"], allow=allow) == []
    allow = {("tests/other.py", "litellm")}   # 只豁免列出的檔案
    assert _rules(check_boundary(repo, ["tests/sample"], allow=allow)) == ["locate", "src-path"]


def _src_paths(vs: list[Violation]) -> list[str]:
    return sorted(v.detail for v in vs if v.rule == "src-path")


@pytest.mark.parametrize("code,allowed,remaining", [
    # 允許完整的 models.yaml 路徑,不放行同檔的另一條 `/ "src"` 路徑
    ("from pathlib import Path\nR = Path('.')\nM = R / 'src' / 'spillway' / 'adapters' / 'claude_cli' / 'models.yaml'\n"
     "X = R / 'src' / 'spillway' / 'router.py'\n",
     "R / 'src' / 'spillway' / 'adapters' / 'claude_cli' / 'models.yaml'", ["R / 'src' / 'spillway' / 'router.py'"]),
    # 允許 `R / 'src'`,不放行以它為前綴的更長路徑
    ("from pathlib import Path\nR = Path('.')\nP = str(R / 'src')\nX = R / 'src' / 'spillway' / 'x.py'\n",
     "R / 'src'", ["R / 'src' / 'spillway' / 'x.py'"]),
    # 允許一條 os.path.join,不放行另一條
    ("import os\nA = os.path.join('r', 'src', 'spillway', 'adapters')\nB = os.path.join('r', 'src', 'spillway', 'app.py')\n",
     "os.path.join('r', 'src', 'spillway', 'adapters')", ["os.path.join('r', 'src', 'spillway', 'app.py')"]),
])
def test_allowlist_src_path_matches_whole_expression(tmp_path, code, allowed, remaining):
    """終審 F1:`src-path` 的字面值是整個路徑運算式(最外層 `/` 鏈或整個呼叫),允許一條不放行其他條。"""
    repo = _repo(tmp_path, {"tests/sample/test_a.py": code})
    everything = check_boundary(repo, ["tests/sample"], allow=set())
    assert allowed in _src_paths(everything) and set(remaining) <= set(_src_paths(everything))
    got = check_boundary(repo, ["tests/sample"], allow={("tests/sample/test_a.py", allowed)})
    assert _src_paths(got) == sorted(remaining), [str(v) for v in got]
    # 單一 `"src"` 片段不再是可用的字面值
    assert _src_paths(check_boundary(repo, ["tests/sample"], allow={("tests/sample/test_a.py", "src")})) \
        == _src_paths(everything)


def test_src_path_flags_outermost_chain_once(tmp_path):
    """`/` 鏈只判最外層一次;`"src"` 在鏈的中段(右側不是 `"src"`)也要判。"""
    repo = _repo(tmp_path, {"tests/sample/test_a.py":
                            "from pathlib import Path\nR = Path('.')\nX = R / 'src' / 'spillway' / 'app.py'\n"})
    assert _src_paths(check_boundary(repo, ["tests/sample"], allow=set())) == ["R / 'src' / 'spillway' / 'app.py'"]


def test_allowlist_entries_have_reasons(tmp_path):
    bad = tmp_path / "allow.toml"
    bad.write_text('[[allow]]\nfile = "a.py"\nliteral = "x"\n', encoding="utf-8")
    with pytest.raises(AssertionError, match="理由"):
        load_allowlist(bad)
    bad.write_text('[[allow]]\nfile = "a.py"\nliteral = "x"\npattern = "y"\nreason = "r"\n', encoding="utf-8")
    with pytest.raises(AssertionError, match="剛好一個"):
        load_allowlist(bad)
    assert load_allowlist()   # 實際的允許清單可解析,且每項都有理由


# ============================================================================ 實際範圍


def test_converted_roots_exist():
    roots = converted_roots()
    assert roots[0] == "tests/blackbox"
    for r in roots:
        assert (REPO_ROOT / r).is_dir(), f"{CONVERTED_ROOTS_FILE.name} 列出不存在的目錄:{r}"


def test_repo_blackbox_boundary():
    violations = check_boundary(REPO_ROOT, converted_roots())
    assert violations == [], "黑箱邊界違規:\n" + "\n".join(map(str, violations))

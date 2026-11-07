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
   例外列在 `tests/blackbox/boundary_allowlist.toml`(檔案、字面值、理由)。
4. 字樣(AM-14,範圍內的檔案):字串常數(docstring 除外)不得含 `uvicorn` 或 `INFO:`;不得 import `uvicorn`。

L1 以暫存目錄中的違規樣本(直接 import、經 `tests.helpers` 間接 import、字面 `src/spillway/` 路徑、`uvicorn`
字樣等)驗證守門會失敗,並以合規樣本驗證會通過;最後對本 repo 的實際範圍執行。
"""
from __future__ import annotations

import ast
import re
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
LOCATOR_CALLS = IMPORT_CALLS | {"find_spec", "files"}
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
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out = set()
    for e in data.get("allow", []):
        assert e.get("reason"), f"允許清單的項目必須寫明理由:{e}"
        out.add((e["file"], e["literal"]))
    return out


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


def _literal_violations(rel: str, tree: ast.AST, allow: set[tuple[str, str]]) -> list[Violation]:
    out: list[Violation] = []
    docs = _docstring_nodes(tree)

    def flag(rule: str, literal: str) -> None:
        if (rel, literal) not in allow:
            out.append(Violation(rel, rule, literal))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            if _SRC_PATH.search(node.value):
                flag("src-path", node.value)
            for w in _WORDS:
                if w in node.value:
                    flag("word", node.value)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) \
                and isinstance(node.right, ast.Constant) and node.right.value == "src":
            flag("src-path", "src")
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            args = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if name == "join" and "src" in args:
                flag("src-path", "src")
            if name in LOCATOR_CALLS and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str) and _FORBIDDEN_ARG.match(node.args[0].value):
                flag("locate", node.args[0].value)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            if any(m.split(".")[0] == "uvicorn" for m in mods):
                flag("word", "import uvicorn")
    return out


# ---------------------------------------------------------------------------- 檢查


def check_boundary(repo: Path, roots: list[str], allow: set[tuple[str, str]] | None = None) -> list[Violation]:
    allow = load_allowlist() if allow is None else allow
    roots = [r.rstrip("/") for r in roots]
    starts: list[Path] = []
    for r in roots:
        d = repo / r
        if d.is_dir():
            starts += sorted(p for p in d.rglob("*.py") if "__pycache__" not in p.parts)
    implicit = [p for p in (repo / "tests" / "conftest.py",) if p.is_file()]

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
        for name in imports_of(repo, path, tree, load_time_only=path in implicit):
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
CMD = [sys.executable, "-m", "spillway"]


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


def test_allowlist_entries_have_reasons(tmp_path):
    bad = tmp_path / "allow.toml"
    bad.write_text('[[allow]]\nfile = "a.py"\nliteral = "x"\n', encoding="utf-8")
    with pytest.raises(AssertionError, match="理由"):
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

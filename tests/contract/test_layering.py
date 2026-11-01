"""Adapter 模組分層檢查(spec 7.6.3;design 規則 2;Global Constraints「分層」)。

白名單:`spillway.adapters.<module>` 只可 import `spillway.adapters.base`、自己模組內的檔案、
標準庫(`sys.stdlib_module_names`)與 `yaml`(pyyaml)。不可 import 其他 `spillway.*`(credentials、
clock、routing、store、api、mcp、其他 adapter 模組),也不可 import 任何其他第三方套件:
`litellm` 一律經 `adapters.base.litellm_call`,HTTP 探測經 `adapters.base.probe_http`。
以 AST 掃描,不執行模組程式。

另含 SQL 位置檢查(CHG-20261101-02 T3,檔尾):SQL 只可出現在 `src/spillway/store/`。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import spillway
import spillway.adapters as adapters_pkg
from spillway.adapters.base import layering_violations

ADAPTERS_DIR = Path(adapters_pkg.__file__).parent
MODULE_DIRS = sorted(
    d for d in ADAPTERS_DIR.iterdir()
    if d.is_dir() and d.name != "base" and not d.name.startswith((".", "_")) and (d / "__init__.py").is_file()
)


@pytest.mark.parametrize("module_dir", MODULE_DIRS, ids=[d.name for d in MODULE_DIRS])
def test_builtin_module_layering(module_dir):
    assert layering_violations(module_dir, f"spillway.adapters.{module_dir.name}") == []


# ---- 機制本身:以 tmp 假模組證明會報錯 ---------------------------------------------------

def _mod(tmp_path, files: dict[str, str]) -> Path:
    d = tmp_path / "fake_mod"
    d.mkdir()
    for rel, src in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
    return d


def _check(tmp_path, src: str, rel: str = "adapter.py") -> list[str]:
    d = _mod(tmp_path, {"__init__.py": "", rel: src})
    return layering_violations(d, "spillway.adapters.fake_mod")


@pytest.mark.parametrize(
    "src",
    [
        "import spillway.credentials\n",
        "from spillway.credentials import resolve\n",
        "from spillway import credentials\n",
        "import spillway.clock\n",
        "from spillway.routing import router\n",
        "from spillway.store.repo import x\n",
        "from spillway.adapters import openai_api\n",
        "from spillway.adapters.openai_api import adapter\n",
        "import spillway\n",
        "import litellm\n",
        "import litellm.exceptions\n",
        "from litellm import acompletion\n",
        "from ... import credentials\n",
        "from ...routing import router\n",
        "from ..openai_api import adapter\n",
        "import importlib\nimportlib.import_module('spillway.credentials')\n",
        "__import__('litellm')\n",
        "def f():\n    import spillway.store\n",
        "import httpx\n",
        "from httpx import AsyncClient\n",
        "import requests\n",
        "import pydantic\n",
        "import fastapi\n",
        "import yaml_extras\n",
        "import importlib\nimportlib.import_module('httpx')\n",
    ],
)
def test_forbidden_imports_are_reported(tmp_path, src):
    violations = _check(tmp_path, src)
    assert violations, src
    assert "adapter.py" in violations[0]


@pytest.mark.parametrize(
    "src",
    [
        "from spillway.adapters.base import CallResult\n",
        "import spillway.adapters.base\n",
        "from spillway.adapters.base.types import Deployment\n",
        "from spillway.adapters import base\n",
        "from ..base import CallResult\n",
        "from .. import base\n",
        "from .helper import x\n",
        "from . import helper\n",
        "import spillway.adapters.fake_mod.helper\n",
        "import yaml\nimport json\nimport re\nimport asyncio\n",
        "from yaml import safe_load\nimport yaml.constructor\n",
        "from __future__ import annotations\nimport os.path\nfrom collections.abc import Mapping\n",
        "import importlib\nimportlib.import_module('.helper', __package__)\n",
        "from spillway.adapters.base import probe_http\n",
    ],
)
def test_allowed_imports_pass(tmp_path, src):
    assert _check(tmp_path, src) == []


def test_nested_subpackage_relative_resolution(tmp_path):
    d = _mod(tmp_path, {"__init__.py": "", "sub/__init__.py": "", "sub/x.py": "from ...base import CallResult\n"})
    assert layering_violations(d, "spillway.adapters.fake_mod") == []
    (d / "sub" / "y.py").write_text("from ...routing import r\n", encoding="utf-8")
    assert len(layering_violations(d, "spillway.adapters.fake_mod")) == 1


def test_violation_in_init_is_reported(tmp_path):
    d = _mod(tmp_path, {"__init__.py": "from spillway import credentials\n"})
    v = layering_violations(d, "spillway.adapters.fake_mod")
    assert len(v) == 1 and "__init__.py" in v[0] and "spillway.credentials" in v[0]


def test_syntax_error_is_reported(tmp_path):
    d = _mod(tmp_path, {"__init__.py": "def (:\n"})
    assert layering_violations(d, "spillway.adapters.fake_mod")


# ================================================================ SQL 只在 src/spillway/store/
# CHG-20261101-02 Global Constraints「分層」:AST 中不是 docstring 的字串常數、去掉前導空白後以
# SELECT/INSERT/UPDATE/DELETE/WITH/PRAGMA 開頭(不分大小寫,後接空白),或 `.execute(`、
# `.executemany(`、`.executescript(` 呼叫,只可出現在 src/spillway/store/ 之下。

SRC_DIR = Path(spillway.__file__).parent
STORE_DIR = SRC_DIR / "store"
_SQL_PREFIX = re.compile(r"(?:SELECT|INSERT|UPDATE|DELETE|WITH|PRAGMA)\s", re.IGNORECASE)
_EXEC_ATTRS = frozenset({"execute", "executemany", "executescript"})
_DOC_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """模組、類別、函式本體第一個敘述若為字串常數即為 docstring,回傳這些節點的 id。"""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, _DOC_OWNERS) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                ids.add(id(first.value))
    return ids


def sql_violations(root: Path, allowed: Path | None = None) -> list[str]:
    """掃描 `root` 下所有 .py(跳過 `allowed` 目錄),回傳 `路徑:行: 原因` 清單。"""
    out: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if allowed is not None and path.is_relative_to(allowed):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as e:
            out.append(f"{path}:{e.lineno}: syntax error")
            continue
        docs = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docs and _SQL_PREFIX.match(node.value.lstrip())):
                out.append(f"{path}:{node.lineno}: SQL 字串 {node.value.strip()[:40]!r}")
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _EXEC_ATTRS):
                out.append(f"{path}:{node.lineno}: .{node.func.attr}( 呼叫")
    return out


def test_sql_only_in_store():
    assert sql_violations(SRC_DIR, allowed=STORE_DIR) == []


def test_store_itself_has_sql():
    # 檢查器確實掃到 store 內的 SQL(避免上一個測試因掃描失效而空過)
    assert sql_violations(STORE_DIR)


def _scan(tmp_path, src: str) -> list[str]:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text(src, encoding="utf-8")
    store = pkg / "store"
    store.mkdir()
    (store / "repo.py").write_text('conn.execute("SELECT 1")\n', encoding="utf-8")
    return sql_violations(pkg, allowed=store)


@pytest.mark.parametrize("src", [
    'def f(conn):\n    conn.execute("SELECT 1")\n',
    'x = "SELECT 1"\n',
    'x = "  select id FROM t"\n',
    'x = "\\nINSERT INTO t VALUES (1)"\n',
    'x = "update t SET a = 1"\n',
    'x = "Delete FROM t"\n',
    'x = "WITH a AS (SELECT 1) SELECT * FROM a"\n',
    'x = "PRAGMA foreign_keys"\n',
    'x = f"SELECT {cols} FROM t"\n',
    'def f(conn):\n    conn.executemany(sql, rows)\n',
    'def f(conn):\n    conn.executescript(script)\n',
    'def f(cur):\n    cur.execute(q)\n',
    'def f():\n    """doc"""\n    return "DELETE FROM t"\n',
    'class C:\n    x = """SELECT 1"""\n',
], ids=["execute-call", "select", "lower-leading-space", "insert-newline", "update", "delete",
        "with", "pragma", "fstring", "executemany", "executescript", "cursor-execute",
        "return-after-doc", "class-attr"])
def test_sql_checker_flags(tmp_path, src):
    v = _scan(tmp_path, src)
    assert v, src
    assert all("mod.py" in x for x in v)


@pytest.mark.parametrize("src", [
    '"""DELETE /api/deployments"""\n',
    'def f():\n    """DELETE /api/deployments"""\n',
    'async def f():\n    """SELECT 1 is how you probe"""\n',
    'class C:\n    """UPDATE semantics: partial."""\n',
    'x = "selected"\n',
    'x = "DELETED"\n',
    'x = "update"\n',
    'x = "withdraw funds"\n',
    'x = "成員 SELECT 1"\n',
    'def f(e):\n    e.run("x")\n',
], ids=["module-doc", "func-doc", "async-doc", "class-doc", "selected", "deleted-no-space",
        "bare-keyword", "with-prefix-word", "not-leading", "other-call"])
def test_sql_checker_does_not_flag(tmp_path, src):
    assert _scan(tmp_path, src) == []

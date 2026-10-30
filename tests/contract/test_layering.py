"""Adapter 模組分層檢查(spec 7.6.3;design 規則 2;Global Constraints「分層」)。

`spillway.adapters.<module>` 只可 import `spillway.adapters.base`、自己模組內的檔案、
標準庫與第三方套件;不可 import 其他 `spillway.*`(credentials、clock、routing、store、
api、mcp、其他 adapter 模組),也不可直接 import `litellm`(一律經 `adapters.base.litellm_call`)。
以 AST 掃描,不執行模組程式。
"""
from __future__ import annotations

from pathlib import Path

import pytest

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
        "import httpx\n",
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

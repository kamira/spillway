"""`api.admin.common.invalid` 與 `config_http` 更名(CHG-20261103-01 T1;決策 21 ①、②)。"""
from __future__ import annotations

import re
from pathlib import Path

from spillway.api import ApiError
from spillway.api.admin.common import invalid

ROOT = Path(__file__).resolve().parents[3]


def _py_hits(base: str, pattern: str) -> list[str]:
    """只掃 `*.py` 原始碼(不含 `__pycache__` 的殘留 .pyc)。"""
    rx = re.compile(pattern)
    return [f"{f}:{n}" for f in sorted((ROOT / base).rglob("*.py"))
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1) if rx.search(line)]


def test_invalid_without_path_is_reason():
    e = invalid(None, "x")
    assert isinstance(e, ApiError)
    assert (e.status, e.code, e.message) == (422, "validation_error", "x")


def test_invalid_with_path_prefixes():
    e = invalid("a.b", "x")
    assert (e.status, e.code, e.message) == (422, "validation_error", "a.b: x")


def test_no_legacy_422_helpers_left():
    assert _py_hits("src/spillway/api/admin", r"_bad_request|def _invalid|import.*_invalid") == []


def test_config_io_module_renamed():
    assert not (ROOT / "src/spillway/api/admin/config_io.py").exists()
    assert (ROOT / "src/spillway/api/admin/config_http.py").exists()
    assert _py_hits("src", r"config_io") == []

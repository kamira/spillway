"""`api.admin.common.invalid` 與 `config_http` 更名(CHG-20261103-01 T1;決策 21 ①、②)。"""
from __future__ import annotations

import subprocess
from pathlib import Path

from spillway.api import ApiError
from spillway.api.admin.common import invalid

ROOT = Path(__file__).resolve().parents[3]


def test_invalid_without_path_is_reason():
    e = invalid(None, "x")
    assert isinstance(e, ApiError)
    assert (e.status, e.code, e.message) == (422, "validation_error", "x")


def test_invalid_with_path_prefixes():
    e = invalid("a.b", "x")
    assert (e.status, e.code, e.message) == (422, "validation_error", "a.b: x")


def test_no_legacy_422_helpers_left():
    r = subprocess.run(
        ["grep", "-rn", r"_bad_request\|def _invalid\|import.*_invalid", "src/spillway/api/admin"],
        cwd=ROOT, capture_output=True, text=True)
    assert r.stdout == ""


def test_config_io_module_renamed():
    assert not (ROOT / "src/spillway/api/admin/config_io.py").exists()
    assert (ROOT / "src/spillway/api/admin/config_http.py").exists()
    r = subprocess.run(["grep", "-rn", "config_io", "src"], cwd=ROOT, capture_output=True, text=True)
    assert r.stdout == ""

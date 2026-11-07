"""`scripts/check_venv_integrity.py` 的單元測試(CHG-20261107-01 T1;決策 6 ⑤)。

在 tmp 目錄建假的 venv:乾淨、多一個 `.pth`、有 `sitecustomize`、RECORD 雜湊不符、有不屬於任何 RECORD
的檔案,各自斷言結束碼與輸出。腳本以子行程 `python -I` 執行(與實際用法相同)。
"""
from __future__ import annotations

import base64
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_venv_integrity.py"


def _sha(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def make_venv(root: Path) -> Path:
    sp = root / "lib" / "python3.13" / "site-packages"
    (sp / "pkg").mkdir(parents=True)
    (sp / "pkg-1.0.dist-info").mkdir()
    body = b"x = 1\n"
    (sp / "pkg" / "__init__.py").write_bytes(body)
    (sp / "__editable__.spillway-0.1.0.pth").write_text("/src\n")
    (sp / "__pycache__").mkdir()
    (sp / "pkg-1.0.dist-info" / "RECORD").write_text(
        f"pkg/__init__.py,{_sha(body)},6\npkg-1.0.dist-info/RECORD,,\n"
        "__editable__.spillway-0.1.0.pth,,\n", encoding="utf-8")
    return root


def run(venv: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", str(SCRIPT), str(venv)], capture_output=True, text=True, check=False)


def test_clean_venv(tmp_path: Path):
    r = run(make_venv(tmp_path))
    assert (r.returncode, r.stdout.strip()) == (0, "沒有發現")


@pytest.mark.parametrize(("tamper", "expect"), [
    (lambda sp: (sp / "evil.pth").write_text("import os\n"), "不允許的 .pth"),
    (lambda sp: (sp / "sitecustomize.py").write_text("import os\n"), "不應存在的啟動腳本"),
    (lambda sp: (sp / "pkg" / "__init__.py").write_bytes(b"x = 2\n"), "雜湊不符"),
    (lambda sp: (sp / "stray.py").write_text("pass\n"), "不屬於任何 RECORD 的項目"),
])
def test_findings(tmp_path: Path, tamper, expect: str):
    venv = make_venv(tmp_path)
    tamper(venv / "lib" / "python3.13" / "site-packages")
    r = run(venv)
    assert r.returncode == 1
    assert expect in r.stdout


def test_usercustomize_in_lib_dir(tmp_path: Path):
    venv = make_venv(tmp_path)
    (venv / "lib" / "python3.13" / "usercustomize.py").write_text("import os\n")
    r = run(venv)
    assert r.returncode == 1 and "usercustomize.py" in r.stdout


def test_missing_site_packages(tmp_path: Path):
    r = run(tmp_path)
    assert r.returncode == 1 and "找不到 site-packages" in r.stdout

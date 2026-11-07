"""`scripts/check_venv_integrity.py` 的單元測試(CHG-20261107-01 T1;決策 6 ⑤,施工審議修訂 F1–F4、F8)。

在 tmp 目錄建假的 venv,涵蓋乾淨、不允許的 `.pth`、允許名稱但含 `import` 行的 `.pth`、`.pth` 不在 spillway 的
RECORD、各種形式的 `sitecustomize`、RECORD 雜湊不符、RECORD 列無雜湊、RECORD 列的檔案不存在、不屬於任何
RECORD 的檔案(含子目錄);腳本以子行程 `python -I -S` 執行(與實際用法相同),只用 `-I` 時拒絕執行。
"""
from __future__ import annotations

import base64
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_venv_integrity.py"
PTH = "__editable__.spillway-0.1.0.pth"


def _sha(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def _site(venv: Path) -> Path:
    return venv / "lib" / "python3.13" / "site-packages"


def _make_venv(root: Path) -> Path:
    sp = _site(root)
    (sp / "pkg").mkdir(parents=True)
    (sp / "pkg-1.0.dist-info").mkdir()
    (sp / "spillway-0.1.0.dist-info").mkdir()
    (sp / "__pycache__").mkdir()
    body, pth = b"x = 1\n", b"/src\n"
    (sp / "pkg" / "__init__.py").write_bytes(body)
    (sp / PTH).write_bytes(pth)
    (sp / "pkg-1.0.dist-info" / "RECORD").write_text(
        f"pkg/__init__.py,{_sha(body)},6\npkg-1.0.dist-info/RECORD,,\n", encoding="utf-8")
    (sp / "spillway-0.1.0.dist-info" / "RECORD").write_text(
        f"{PTH},{_sha(pth)},5\nspillway-0.1.0.dist-info/RECORD,,\n", encoding="utf-8")
    return root


def _run(venv: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, *(flags or ("-I", "-S")), str(SCRIPT), str(venv)],
                          capture_output=True, text=True, check=False)


def _add_to_record(sp: Path, rel: str, data: bytes) -> None:
    rec = sp / "pkg-1.0.dist-info" / "RECORD"
    rec.write_text(rec.read_text(encoding="utf-8") + f"{rel},{_sha(data)},{len(data)}\n", encoding="utf-8")


def test_clean_venv(tmp_path: Path):
    r = _run(_make_venv(tmp_path))
    assert (r.returncode, r.stdout.strip()) == (0, "沒有發現")


def test_refuses_without_no_site(tmp_path: Path):
    r = _run(_make_venv(tmp_path), "-I")
    assert r.returncode == 2 and "-I -S" in r.stderr


def _evil_pth(sp: Path) -> None:
    (sp / "evil.pth").write_text("import os\n")


def _allowed_name_with_import(sp: Path) -> None:
    data = b"import os; os.system('x')\n"
    (sp / PTH).write_bytes(data)
    rec = sp / "spillway-0.1.0.dist-info" / "RECORD"
    rec.write_text(f"{PTH},{_sha(data)},{len(data)}\nspillway-0.1.0.dist-info/RECORD,,\n", encoding="utf-8")


def _pth_not_in_spillway_record(sp: Path) -> None:
    (sp / "spillway-0.1.0.dist-info" / "RECORD").write_text("spillway-0.1.0.dist-info/RECORD,,\n", encoding="utf-8")
    _add_to_record(sp, PTH, (sp / PTH).read_bytes())


def _hook_package(sp: Path) -> None:
    (sp / "sitecustomize").mkdir()
    (sp / "sitecustomize" / "__init__.py").write_bytes(b"import os\n")
    _add_to_record(sp, "sitecustomize/__init__.py", b"import os\n")


def _blank_hash(sp: Path) -> None:
    rec = sp / "pkg-1.0.dist-info" / "RECORD"
    (sp / "pkg" / "__init__.py").write_bytes(b"x = 2\n")
    rec.write_text("pkg/__init__.py,,\npkg-1.0.dist-info/RECORD,,\n", encoding="utf-8")


@pytest.mark.parametrize(("tamper", "expect"), [
    (_evil_pth, "不允許的 .pth"),
    (_allowed_name_with_import, ".pth 含可執行行"),
    (_pth_not_in_spillway_record, ".pth 不在 spillway 的 RECORD"),
    (lambda sp: (sp / "sitecustomize.py").write_text("import os\n"), "不應存在的啟動掛鉤"),
    (lambda sp: (sp / "sitecustomize.pyc").write_bytes(b"\0"), "不應存在的啟動掛鉤"),
    (lambda sp: (sp / "sitecustomize.cpython-313-darwin.so").write_bytes(b"\0"), "不應存在的啟動掛鉤"),
    (_hook_package, "不應存在的啟動掛鉤"),
    (lambda sp: (sp / "pkg" / "__init__.py").write_bytes(b"x = 2\n"), "雜湊不符"),
    (_blank_hash, "RECORD 中沒有雜湊的檔案"),
    (lambda sp: (sp / "pkg" / "__init__.py").unlink(), "RECORD 列出的檔案不存在"),
    (lambda sp: (sp / "stray.py").write_text("pass\n"), "不屬於任何 RECORD 的檔案"),
    (lambda sp: (sp / "pkg" / "extra.pyc").write_bytes(b"\0"), "不屬於任何 RECORD 的檔案"),
])
def test_findings(tmp_path: Path, tamper, expect: str):
    venv = _make_venv(tmp_path)
    tamper(_site(venv))
    r = _run(venv)
    assert r.returncode == 1
    assert expect in r.stdout


def test_pycache_is_not_checked(tmp_path: Path):
    venv = _make_venv(tmp_path)
    (_site(venv) / "pkg" / "__pycache__").mkdir()
    (_site(venv) / "pkg" / "__pycache__" / "__init__.cpython-313.pyc").write_bytes(b"\0")
    assert _run(venv).returncode == 0          # 已知限制:`__pycache__` 不驗證


def test_usercustomize_in_lib_dir(tmp_path: Path):
    venv = _make_venv(tmp_path)
    (venv / "lib" / "python3.13" / "usercustomize.py").write_text("import os\n")
    r = _run(venv)
    assert r.returncode == 1 and "usercustomize.py" in r.stdout


def test_missing_site_packages(tmp_path: Path):
    r = _run(tmp_path)
    assert r.returncode == 1 and "找不到 site-packages" in r.stdout

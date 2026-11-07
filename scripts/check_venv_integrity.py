"""venv 的入侵檢查(CHG-20261107-01 決策 6 ⑤,決策審議修訂 AM-8)。

以 `python3 -I scripts/check_venv_integrity.py .venv` 執行(系統 python,或 `.venv/bin/python -I -S`);只用標準庫,
不 import venv 內的任何套件、不處理 `.pth`。檢查:
1. site-packages 的 `*.pth` 只能是 `__editable__.spillway-*.pth`;
2. 沒有 `sitecustomize.py`、`usercustomize.py`(site-packages 頂層與 venv 的 `lib/python*/`);
3. 每個 `*.dist-info/RECORD` 中有 sha256 的檔案,實際雜湊都相符;
4. site-packages 頂層每個檔案或目錄都屬於某個 RECORD(`__pycache__` 除外)。
有發現時結束碼 1 並列出路徑;沒有時結束碼 0。
"""
from __future__ import annotations

import base64
import csv
import fnmatch
import hashlib
import sys
from pathlib import Path

CUSTOMIZE = ("sitecustomize.py", "usercustomize.py")
ALLOWED_PTH = "__editable__.spillway-*.pth"


def site_packages(venv: Path) -> list[Path]:
    return sorted(p for p in (venv / "lib").glob("python*/site-packages") if p.is_dir())


def _digest(path: Path) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest()).rstrip(b"=").decode()


def check(venv: Path) -> list[str]:
    problems: list[str] = []
    sps = site_packages(venv)
    if not sps:
        return [f"找不到 site-packages:{venv}/lib/python*/site-packages"]
    for lib in sorted((venv / "lib").glob("python*")):
        for name in CUSTOMIZE:
            if (lib / name).exists():
                problems.append(f"不應存在的啟動腳本:{lib / name}")
    for sp in sps:
        for pth in sorted(sp.glob("*.pth")):
            if not fnmatch.fnmatch(pth.name, ALLOWED_PTH):
                problems.append(f"不允許的 .pth:{pth}")
        for name in CUSTOMIZE:
            if (sp / name).exists():
                problems.append(f"不應存在的啟動腳本:{sp / name}")
        owned: set[str] = set()
        for rec in sorted(sp.glob("*.dist-info/RECORD")):
            for row in csv.reader(rec.open(encoding="utf-8", newline="")):
                if not row or not row[0]:
                    continue
                rel = row[0]
                owned.add(rel.split("/")[0])
                if len(row) < 2 or not row[1].startswith("sha256="):
                    continue
                target = sp / rel
                if not target.is_file():
                    continue
                if _digest(target) != row[1][len("sha256="):]:
                    problems.append(f"雜湊不符:{target}(RECORD {rec.parent.name})")
        for entry in sorted(sp.iterdir()):
            if entry.name != "__pycache__" and entry.name not in owned:
                problems.append(f"不屬於任何 RECORD 的項目:{entry}")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法:python3 -I scripts/check_venv_integrity.py <venv 目錄>", file=sys.stderr)
        return 2
    problems = check(Path(argv[1]))
    for p in problems:
        print(p)
    print(f"發現 {len(problems)} 項" if problems else "沒有發現")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

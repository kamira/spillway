"""venv 的入侵檢查(CHG-20261107-01 決策 6 ⑤,決策審議修訂 AM-8;施工審議修訂 F1–F4、F8)。

以 `python3 -I -S scripts/check_venv_integrity.py .venv` 執行(系統 python,或 `.venv/bin/python -I -S`);
未加 `-S` 時拒絕執行(結束碼 2),因為啟用 venv 時 `python3` 就是 venv 的 python,`.pth` 會在檢查前執行。
只用標準庫,不 import venv 內的任何套件。檢查:
1. site-packages 的 `*.pth` 只能是 `__editable__.spillway-*.pth`,必須列在 spillway 的 RECORD,且不得含
   `import` 開頭的可執行行;
2. `lib/python*/` 與 site-packages 頂層沒有任何形式的 `sitecustomize`、`usercustomize`(.py、.pyc、.so、套件目錄);
3. RECORD 中只有 `*.dist-info/RECORD` 自身可以沒有雜湊;有雜湊的檔案必須存在且雜湊相符;
4. site-packages 內每個檔案(`__pycache__` 除外)都屬於某個 RECORD。
有發現時結束碼 1 並列出路徑;沒有時結束碼 0;用法錯誤或未加 `-S` 時結束碼 2。
目的是抓已知的啟動掛鉤與安裝後的檔案修改;不防已能任意寫入 venv 的人(他也能改這支腳本)。
"""
from __future__ import annotations

import base64
import csv
import fnmatch
import hashlib
import sys
from pathlib import Path

__all__ = ["check", "main", "site_packages"]

HOOKS = frozenset({"sitecustomize", "usercustomize"})
ALLOWED_PTH = "__editable__.spillway-*.pth"


def site_packages(venv: Path) -> list[Path]:
    """venv 內所有 `lib/python*/site-packages` 目錄。"""
    return sorted(p for p in (venv / "lib").glob("python*/site-packages") if p.is_dir())


def _digest(path: Path) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest()).rstrip(b"=").decode()


def _hooks_in(directory: Path) -> list[str]:
    return [f"不應存在的啟動掛鉤:{e}" for e in sorted(directory.iterdir()) if e.name.split(".")[0] in HOOKS]


def _check_site(sp: Path) -> list[str]:
    problems = _hooks_in(sp)
    owned: set[Path] = set()
    spillway_owned: set[str] = set()
    for rec in sorted(sp.glob("*.dist-info/RECORD")):
        for row in csv.reader(rec.open(encoding="utf-8", newline="")):
            if not row or not row[0]:
                continue
            rel = row[0]
            target = (sp / rel).resolve()
            owned.add(target)
            if rec.parent.name.startswith("spillway-"):
                spillway_owned.add(rel)
            digest = row[1] if len(row) > 1 else ""
            if not digest:
                if rel != f"{rec.parent.name}/RECORD":
                    problems.append(f"RECORD 中沒有雜湊的檔案:{sp / rel}(RECORD {rec.parent.name})")
                continue
            if not digest.startswith("sha256="):
                problems.append(f"RECORD 的雜湊不是 sha256:{sp / rel}")
                continue
            if not target.is_file():
                problems.append(f"RECORD 列出的檔案不存在:{sp / rel}")
            elif _digest(target) != digest[len("sha256="):]:
                problems.append(f"雜湊不符:{sp / rel}(RECORD {rec.parent.name})")
    for pth in sorted(sp.glob("*.pth")):
        if not fnmatch.fnmatch(pth.name, ALLOWED_PTH):
            problems.append(f"不允許的 .pth:{pth}")
        elif pth.name not in spillway_owned:
            problems.append(f".pth 不在 spillway 的 RECORD:{pth}")
        lines = pth.read_text(encoding="utf-8", errors="replace").splitlines()
        if any(line.lstrip().startswith(("import ", "import\t")) for line in lines):
            problems.append(f".pth 含可執行行:{pth}")
    for f in sorted(p for p in sp.rglob("*") if p.is_file() or p.is_symlink()):
        if "__pycache__" in f.relative_to(sp).parts:
            continue
        if f.resolve() not in owned:
            problems.append(f"不屬於任何 RECORD 的檔案:{f}")
    for entry in sorted(sp.iterdir()):
        if entry.is_dir() and entry.name != "__pycache__" and not any(entry.iterdir()):
            problems.append(f"不屬於任何 RECORD 的空目錄:{entry}")
    return problems


def check(venv: Path) -> list[str]:
    """回傳全部發現(空清單表示沒有發現)。"""
    sps = site_packages(venv)
    if not sps:
        return [f"找不到 site-packages:{venv}/lib/python*/site-packages"]
    problems: list[str] = []
    for lib in sorted(p for p in (venv / "lib").glob("python*") if p.is_dir()):
        problems += _hooks_in(lib)
    for sp in sps:
        problems += _check_site(sp)
    return problems


def main(argv: list[str]) -> int:
    """命令列進入點;結束碼 0 沒有發現、1 有發現、2 用法錯誤或未加 `-S`。"""
    if not sys.flags.no_site:
        print("請以 python3 -I -S scripts/check_venv_integrity.py <venv 目錄> 執行", file=sys.stderr)
        return 2
    if len(argv) != 2:
        print("用法:python3 -I -S scripts/check_venv_integrity.py <venv 目錄>", file=sys.stderr)
        return 2
    problems = check(Path(argv[1]))
    for p in problems:
        print(p)
    print(f"發現 {len(problems)} 項" if problems else "沒有發現")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

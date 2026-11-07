"""黑箱邊界的執行期守門與守門範圍(CHG-20261107-03 T2;決策 9 ①④)。

- `CONVERTED_ROOTS_FILE`:`tests/blackbox/converted_roots.txt`;`converted_roots() -> list[str]`:
  `tests/blackbox` 加上檔案中列出的目錄(相對於 repo 根目錄、POSIX 分隔)。靜態守門
  (`tests/contract/test_blackbox_boundary.py`)與 pytest 執行期守門(`plugin.py`)共用這份清單;
  wave 3 的 T3–T5 轉換完成時各自加一行,不需要改守門程式。
- `spillway_modules(modules=None) -> list[str]`:`sys.modules`(或給定的對照表)中名稱為 `spillway` 或以
  `spillway.` 開頭者。
- `assert_no_spillway_modules(where, modules=None)`:有任何一個就 `AssertionError`(behave 在 `after_all` 呼叫;pytest 由
  `blackbox_guard` fixture 在 session 結束時呼叫)。
- `FORCE_ENV`:`SPILLWAY_BLACKBOX_GUARD=1` 時 pytest 的守門不看收集範圍、一律生效(驗收的隔離執行用)。
"""
from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

__all__ = ["BLACKBOX_ROOT", "CONVERTED_ROOTS_FILE", "FORCE_ENV", "REPO_ROOT", "assert_no_spillway_modules",
           "converted_roots", "spillway_modules"]

REPO_ROOT = Path(__file__).resolve().parents[2]
BLACKBOX_ROOT = "tests/blackbox"
CONVERTED_ROOTS_FILE = Path(__file__).with_name("converted_roots.txt")
FORCE_ENV = "SPILLWAY_BLACKBOX_GUARD"


def converted_roots(path: Path = CONVERTED_ROOTS_FILE) -> list[str]:
    roots = [BLACKBOX_ROOT]
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            root = line.rstrip("/")
            if root not in roots:
                roots.append(root)
    return roots


def spillway_modules(modules: Mapping[str, object] | None = None) -> list[str]:
    mods = sys.modules if modules is None else modules
    return sorted(m for m in mods if m == "spillway" or m.startswith("spillway."))


def assert_no_spillway_modules(where: str = "", modules: Mapping[str, object] | None = None) -> None:
    found = spillway_modules(modules)
    assert not found, (f"黑箱測試{('(' + where + ')') if where else ''}的行程內載入了 spillway:"
                       f"{found[:10]}{' …' if len(found) > 10 else ''}")

"""黑箱邊界的執行期守門與守門範圍(CHG-20261107-03 T2;決策 9 ①④)。

- `CONVERTED_ROOTS_FILE`:`tests/blackbox/converted_roots.txt`;`converted_roots() -> list[str]`:
  `tests/blackbox` 加上檔案中列出的目錄(相對於 repo 根目錄、POSIX 分隔)。靜態守門
  (`tests/contract/test_blackbox_boundary.py`)與 pytest 執行期守門(`plugin.py`)共用這份清單;
  wave 3 的 T3–T5 轉換完成時各自加一行,不需要改守門程式。
- `spillway_modules(modules=None) -> list[str]`:`sys.modules`(或給定的對照表)中屬於 `FORBIDDEN_MODULES`
  (`spillway`、`litellm`;與靜態守門一致,F2)的模組名。
- `SpillwayBlocker`、`install_blocker()`、`uninstall_blocker()`:`sys.meta_path` 的 import 阻擋器(F2)。
- `assert_no_spillway_modules(where, modules=None)`:有任何一個就 `AssertionError`(behave 在 `after_all` 呼叫;pytest 由
  `blackbox_guard` fixture 在 session 結束時呼叫)。
- `FORCE_ENV`:`SPILLWAY_BLACKBOX_GUARD=1` 時 pytest 的守門不看收集範圍、一律生效(驗收的隔離執行用)。
"""
from __future__ import annotations

import contextlib
import importlib.abc
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = ["BLACKBOX_ROOT", "CONVERTED_ROOTS_FILE", "FORBIDDEN_MODULES", "FORCE_ENV", "REPO_ROOT", "SpillwayBlocker",
           "assert_no_spillway_modules", "converted_roots", "install_blocker", "spillway_modules",
           "uninstall_blocker"]

REPO_ROOT = Path(__file__).resolve().parents[2]
BLACKBOX_ROOT = "tests/blackbox"
CONVERTED_ROOTS_FILE = Path(__file__).with_name("converted_roots.txt")
FORCE_ENV = "SPILLWAY_BLACKBOX_GUARD"
FORBIDDEN_MODULES = ("spillway", "litellm")


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
    return sorted(m for m in mods if any(m == t or m.startswith(t + ".") for t in FORBIDDEN_MODULES))


class SpillwayBlocker(importlib.abc.MetaPathFinder):
    """`sys.meta_path` 最前面的 finder:`spillway`/`spillway.*` 一律 ImportError(黑箱邊界)。"""

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
        if fullname == "spillway" or fullname.startswith("spillway."):
            raise ImportError(f"黑箱邊界:黑箱測試的行程內不可 import {fullname}(經 SPILLWAY_CMD 子行程操作)",
                              name=fullname)
        return None


_BLOCKER = SpillwayBlocker()


def install_blocker() -> None:
    if _BLOCKER not in sys.meta_path:
        sys.meta_path.insert(0, _BLOCKER)


def uninstall_blocker() -> None:
    with contextlib.suppress(ValueError):
        sys.meta_path.remove(_BLOCKER)


def assert_no_spillway_modules(where: str = "", modules: Mapping[str, object] | None = None) -> None:
    found = spillway_modules(modules)
    assert not found, (f"黑箱測試{('(' + where + ')') if where else ''}的行程內載入了 "
                       f"{'、'.join(t for t in FORBIDDEN_MODULES if any(m.split('.')[0] == t for m in found))}:"
                       f"{found[:10]}{' …' if len(found) > 10 else ''}")

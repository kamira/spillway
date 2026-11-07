"""`tests/blackbox` 的 fixture(CHG-20261107-03 T2)。

- `_blackbox_guard`(session、autouse):啟用 `tests/blackbox/plugin.py` 的執行期守門——本目錄單獨執行時,
  session 結束時 `sys.modules` 不得有 `spillway*`。轉換後的套件(T3–T5)在各自的 conftest 以同樣方式啟用。
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session", autouse=True)
def _blackbox_guard(blackbox_guard: None) -> Iterator[None]:
    yield

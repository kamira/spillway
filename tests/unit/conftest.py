"""tests/unit 共用 fixture(CHG-20261030-02 T2 擁有)。

`db`:以 `tmp_path` 建立、已套用 schema v1 migration 的 `sqlite3.Connection`(由
`spillway.store.db.connect()` 開啟,`row_factory` 為 `sqlite3.Row`、autocommit 模式)。
測試結束時關閉連線。T7、T9、T10 等讀寫資料庫的單元測試共用此 fixture。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from spillway.store.db import connect


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(tmp_path / "unit-test.db")
    try:
        yield conn
    finally:
        conn.close()

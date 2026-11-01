"""非空資料庫套用 migration v1 的現況(CHG-20261101-02 T10 L1;安全影響 prod-migration;已知限制)。

本檔釘住現況,不改 migration 程式:
- 表名衝突且 `user_version=0`:開啟失敗、交易回滾、原表內容不變、`user_version` 仍為 0。
  `deployments` 是 schema.sql 的第一個 CREATE,衝突在第一條敘述就失敗,證明不了回滾;
  所以另以 schema.sql 最後一個 CREATE 的 `settings` 衝突:失敗前已執行前面所有 CREATE,
  若略過 ROLLBACK 而 COMMIT,`deployments` 等表會殘留,測試即失敗。
- 只含一張無關表且 `user_version=0`:v1 套用成功、無關表保留。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from spillway.store.db import SCHEMA_VERSION, connect


def _raw(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path), isolation_level=None)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_colliding_deployments_table_fails_and_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "collide.db"
    pre = _raw(path)
    pre.execute("CREATE TABLE deployments (id TEXT PRIMARY KEY, note TEXT)")
    pre.execute("INSERT INTO deployments VALUES ('old-1', 'keep me'), ('old-2', 'me too')")
    assert pre.execute("PRAGMA user_version").fetchone()[0] == 0
    pre.close()

    with pytest.raises(sqlite3.OperationalError, match="deployments"):
        connect(path)

    after = _raw(path)
    try:
        assert after.execute("PRAGMA user_version").fetchone()[0] == 0
        # 原表結構與內容不變
        cols = [r[1] for r in after.execute("PRAGMA table_info(deployments)")]
        assert cols == ["id", "note"]
        rows = after.execute("SELECT id, note FROM deployments ORDER BY id").fetchall()
        assert rows == [("old-1", "keep me"), ("old-2", "me too")]
        assert _tables(after) == {"deployments"}
    finally:
        after.close()


def test_colliding_last_v1_table_rolls_back_earlier_creates(tmp_path: Path) -> None:
    path = tmp_path / "collide-late.db"
    pre = _raw(path)
    pre.execute("CREATE TABLE settings (name TEXT PRIMARY KEY, note TEXT)")
    pre.execute("INSERT INTO settings VALUES ('mine', 'keep me')")
    assert pre.execute("PRAGMA user_version").fetchone()[0] == 0
    pre.close()

    with pytest.raises(sqlite3.OperationalError, match="settings"):
        connect(path)

    after = _raw(path)
    try:
        assert after.execute("PRAGMA user_version").fetchone()[0] == 0
        cols = [r[1] for r in after.execute("PRAGMA table_info(settings)")]
        assert cols == ["name", "note"]
        assert after.execute("SELECT name, note FROM settings").fetchall() == [("mine", "keep me")]
        # 衝突前已執行的 v1 CREATE(deployments … usage_payloads)全部回滾,不殘留
        assert _tables(after) == {"settings"}
    finally:
        after.close()


def test_unrelated_table_is_kept_and_v1_applied(tmp_path: Path) -> None:
    path = tmp_path / "unrelated.db"
    pre = _raw(path)
    pre.execute("CREATE TABLE my_notes (k TEXT PRIMARY KEY, v TEXT)")
    pre.execute("INSERT INTO my_notes VALUES ('a', '1')")
    pre.close()

    conn = connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 1
        tables = _tables(conn)
        assert {"my_notes", "deployments", "usage_ledger", "settings"} <= tables
        assert [tuple(r) for r in conn.execute("SELECT k, v FROM my_notes")] == [("a", "1")]
        cols = [r[1] for r in conn.execute("PRAGMA table_info(deployments)")]
        assert "model_name" in cols and "credential_ref" in cols
    finally:
        conn.close()

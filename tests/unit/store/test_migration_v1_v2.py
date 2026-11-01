"""CHG-20261101-03 T1 L2:schema v1 → v2 migration(決策 3、4、5)。

v1 測試資料庫含:一般 budgets 列、含冷卻的列、決策 59 暫行列(type=none、window=none、limit NULL)、
帳本含 quota_exhausted 與 rate_limited 兩種狀態。驗證備份、鎖、資料搬移、失敗回滾。
"""
from __future__ import annotations

import fcntl
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from spillway.clock import FakeClock
from spillway.store import db as store_db
from spillway.store.db import (
    DataDirLockedError,
    MigrationError,
    acquire_data_dir_lock,
    connect,
    release_data_dir_lock,
)

V1_SQL = store_db.MIGRATIONS[0][1]
BUDGET_COLS = "deployment_id, type, limit_amount, window, reset_anchor"


def _raw(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path), isolation_level=None)


def _make_v1(path: Path, *, fk_violation: bool = False) -> None:
    c = _raw(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("BEGIN")
    for stmt in store_db._split_statements(V1_SQL):
        c.execute(stmt)
    c.execute("PRAGMA user_version = 1")
    for dep in ("plain", "cool-rl", "placeholder", "cool-noledger", "nobudget"):
        c.execute("INSERT INTO deployments(id, model_name, kind, provider, model) "
                  "VALUES (?, 'm', 'api', 'openai', 'gpt')", (dep,))
    c.executemany(
        "INSERT INTO budgets(deployment_id, type, limit_amount, window, reset_anchor, exhausted_until) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("plain", "usd", 20.0, "monthly", 5, None),
            ("cool-rl", "usd", 5.0, "daily", None, "2026-10-30T12:01:00Z"),
            ("placeholder", "none", None, "none", None, "2026-10-30T13:00:00Z"),   # 決策 59 暫行列
            ("cool-noledger", "credit_pool", None, "weekly", None, "2026-11-02T00:00:00Z"),
        ],
    )
    c.executemany(
        "INSERT INTO usage_ledger(ts, deployment_id, status) VALUES (?, ?, ?)",
        [
            ("2026-10-30T11:00:00Z", "cool-rl", "quota_exhausted"),
            ("2026-10-30T12:00:00Z", "cool-rl", "rate_limited"),      # 最近一筆 → rate_limited
            ("2026-10-30T12:30:00Z", "cool-rl", "ok"),                # 非冷卻狀態不算
            ("2026-10-30T11:59:00Z", "placeholder", "rate_limited"),
            ("2026-10-30T12:00:00Z", "placeholder", "quota_exhausted"),  # 最近一筆 → quota_exhausted
            ("2026-10-30T12:00:00Z", "cool-noledger", "error"),
        ],
    )
    c.execute("INSERT INTO settings(key, value) VALUES ('payload_retention_days', '7')")
    c.execute("COMMIT")
    if fk_violation:
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute("INSERT INTO deployment_tags(deployment_id, tag) VALUES ('ghost', 'x')")
    c.close()


def _dump(path: Path) -> list[str]:
    c = _raw(path)
    try:
        return list(c.iterdump())
    finally:
        c.close()


def _version(path: Path) -> int:
    c = _raw(path)
    try:
        return c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        c.close()


def _budget_rows(conn: sqlite3.Connection) -> list[tuple]:
    return [tuple(r) for r in conn.execute(f"SELECT {BUDGET_COLS} FROM budgets ORDER BY deployment_id")]


def _baks(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in tmp_path.iterdir() if ".bak" in p.name)


@pytest.fixture
def v1(tmp_path: Path) -> Path:
    path = tmp_path / "spillway.db"
    _make_v1(path)
    return path


def test_v1_database_is_backed_up_and_migrated(v1: Path, tmp_path: Path):
    before_dump = _dump(v1)
    raw = _raw(v1)
    before_budgets = _budget_rows(raw)
    raw.close()

    conn = connect(v1)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        # 備份與原檔(升級前)內容相同
        bak = tmp_path / "spillway.db.v1.bak"
        assert bak.exists()
        assert _dump(bak) == before_dump
        assert _baks(tmp_path) == ["spillway.db.v1.bak"]

        # 冷卻搬到 cooldowns;reason 依帳本最近一筆推得,找不到時 quota_exhausted
        cds = {r["deployment_id"]: (r["exhausted_until"], r["reason"])
               for r in conn.execute("SELECT * FROM cooldowns")}
        assert cds == {
            "cool-rl": ("2026-10-30T12:01:00Z", "rate_limited"),
            "placeholder": ("2026-10-30T13:00:00Z", "quota_exhausted"),
            "cool-noledger": ("2026-11-02T00:00:00Z", "quota_exhausted"),
        }
        # budgets 列(含暫行列)逐欄保留,只少了 exhausted_until
        assert _budget_rows(conn) == [r[:5] for r in before_budgets]
        assert ("placeholder", "none", None, "none", None) in _budget_rows(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(budgets)")]
        assert "exhausted_until" not in cols
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        # 其他資料不動
        assert conn.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0] == 6
        assert conn.execute("SELECT value FROM settings").fetchone()[0] == "7"
    finally:
        conn.close()

    # 遷移後的 DDL 等於全新 v2
    fresh = connect(tmp_path / "fresh.db")
    got = _raw(v1)
    try:
        q = "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
        assert [tuple(r) for r in got.execute(q)] == [tuple(r) for r in fresh.execute(q)]
    finally:
        got.close()
        fresh.close()

    # 再開一次不再備份、不再 migrate;鎖已釋放
    connect(v1).close()
    assert _baks(tmp_path) == ["spillway.db.v1.bak"]
    acquire_data_dir_lock(tmp_path)
    release_data_dir_lock(tmp_path)


def test_existing_backup_is_not_overwritten(v1: Path, tmp_path: Path):
    old = tmp_path / "spillway.db.v1.bak"
    old.write_bytes(b"previous backup")
    clock = FakeClock(datetime(2026, 11, 1, 8, 9, 10, tzinfo=timezone.utc))
    before = _dump(v1)
    connect(v1, clock=clock).close()
    assert old.read_bytes() == b"previous backup"
    stamped = tmp_path / "spillway.db.v1.bak.20261101T080910Z"
    assert stamped.exists()
    assert _dump(stamped) == before


def test_backup_failure_leaves_no_partial_file_and_does_not_migrate(v1, tmp_path, monkeypatch):
    def broken_copy(conn, target):
        Path(target).write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(store_db, "_copy_database", broken_copy)
    with pytest.raises(MigrationError, match="備份失敗") as ei:
        connect(v1)
    assert ei.value.backup_path is None
    assert _baks(tmp_path) == []
    assert _version(v1) == 1


def test_migration_failure_rolls_back_and_reports_backup(v1, tmp_path, monkeypatch):
    before = _dump(v1)
    bad_v2 = store_db.MIGRATIONS[1][1] + "\nCREATE TABLE cooldowns (x INTEGER);\n"
    monkeypatch.setattr(store_db, "MIGRATIONS", (store_db.MIGRATIONS[0], (2, bad_v2)))
    with pytest.raises(MigrationError) as ei:
        connect(v1)
    bak = tmp_path / "spillway.db.v1.bak"
    assert ei.value.backup_path == bak
    assert str(bak) in str(ei.value)
    assert _version(v1) == 1
    assert _dump(v1) == before            # 原檔不變(含 exhausted_until 欄、沒有 cooldowns)
    assert _dump(bak) == before


def test_foreign_key_violation_rolls_back(tmp_path):
    path = tmp_path / "spillway.db"
    _make_v1(path, fk_violation=True)
    with pytest.raises(MigrationError, match="foreign_key_check"):
        connect(path)
    assert _version(path) == 1


def test_locked_data_dir_does_not_migrate(v1, tmp_path):
    fd = os.open(tmp_path / store_db.LOCK_FILENAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # 模擬另一個 serve 持有鎖
        with pytest.raises(DataDirLockedError):
            connect(v1)
        assert _version(v1) == 1
        assert _baks(tmp_path) == []
    finally:
        os.close(fd)
    connect(v1).close()
    assert _version(v1) == 2


def test_lock_is_reentrant_within_process(v1, tmp_path):
    """serve 啟動時先取得並持有鎖(T7 `_preflight`),同行程的 connect 仍可升級。"""
    lock = acquire_data_dir_lock(tmp_path)
    try:
        assert lock == tmp_path.resolve() / "spillway.lock"
        connect(v1).close()
        assert _version(v1) == 2
        # 仍由本行程持有:另開的描述取不到
        fd = os.open(lock, os.O_RDWR)
        try:
            with pytest.raises(OSError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    finally:
        release_data_dir_lock(tmp_path)


def test_empty_database_needs_no_lock_or_backup(tmp_path):
    fd = os.open(tmp_path / store_db.LOCK_FILENAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        connect(tmp_path / "spillway.db").close()
    finally:
        os.close(fd)
    assert _version(tmp_path / "spillway.db") == 2
    assert _baks(tmp_path) == []


def test_concurrent_connects_back_up_once(v1, tmp_path):
    import threading

    n = 6
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            connect(v1).close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=worker) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    assert _version(v1) == 2
    assert _baks(tmp_path) == ["spillway.db.v1.bak"]

"""SQLite 連線、schema migration 與交易輔助(spec 2.1;design D2、D3)。

連線模型(D2):標準庫 `sqlite3`,每請求一條連線、短交易。`connect(path)` 回傳的連線:

- `isolation_level=None`(autocommit):單一敘述立即生效;多敘述要原子時一律用
  `transaction(conn)` 包起來。不依賴 `sqlite3` 模組的隱式 BEGIN,避免讀取也被拖進交易。
- `row_factory = sqlite3.Row`。
- 已執行 `PRAGMA journal_mode=WAL`、`PRAGMA foreign_keys=ON`(spec 2.1.1),
  另設 `busy_timeout` 讓並行寫入短暫等待而不是立刻 `database is locked`。

Migration(D3):以 `PRAGMA user_version` 記版本。`MIGRATIONS` 為 `(版本, SQL)` 依序排列;
`connect()` 對 `user_version` 小於目標版本的庫逐一套用,每個 migration 與其
`user_version` 更新在同一交易內,失敗時整個 migration 回滾、版本不前進。
空庫只建表與索引,不寫入任何資料列(spec 2)。庫的版本比程式新時丟 `SchemaVersionError`。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from itertools import count
from pathlib import Path

BUSY_TIMEOUT_MS = 5000


class SchemaVersionError(RuntimeError):
    """資料庫的 schema 版本比本程式支援的新。"""


def _schema_v1() -> str:
    return resources.files("spillway.store").joinpath("schema.sql").read_text(encoding="utf-8")


MIGRATIONS: tuple[tuple[int, str], ...] = ((1, _schema_v1()),)
SCHEMA_VERSION = MIGRATIONS[-1][0]


def connect(path: str | Path) -> sqlite3.Connection:
    """開啟(必要時建立)資料庫並套用尚未執行的 migration。"""
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    target = MIGRATIONS[-1][0]
    if current > target:
        raise SchemaVersionError(
            f"資料庫 schema 版本 {current} 比程式支援的 {target} 新"
        )
    for version, sql in MIGRATIONS:
        if version > current:
            _apply_migration(conn, version, sql)


def _apply_migration(conn: sqlite3.Connection, version: int, sql: str) -> None:
    # executescript 會先 COMMIT 未完成的交易;自己在腳本內 BEGIN/COMMIT 讓 DDL 與版本號原子更新
    try:
        conn.executescript(
            f"BEGIN IMMEDIATE;\n{sql}\n;PRAGMA user_version = {int(version)};\nCOMMIT;"
        )
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


_savepoint_ids = count(1)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """原子區塊:外層為 `BEGIN IMMEDIATE … COMMIT`;已在交易中時改用 SAVEPOINT,
    讓 repo 函式可以被呼叫端再包進更大的交易。例外時回滾本層並重新丟出。"""
    if conn.in_transaction:
        name = f"sp_{next(_savepoint_ids)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        conn.execute(f"RELEASE {name}")
    else:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

"""SQLite 連線、schema migration 與交易輔助(spec 2.1;design D2、D3)。

連線模型(D2):標準庫 `sqlite3`,每請求一條連線、短交易。`connect(path)` 回傳的連線:

- `isolation_level=None`(autocommit):單一敘述立即生效;多敘述要原子時一律用
  `transaction(conn)` 包起來。不依賴 `sqlite3` 模組的隱式 BEGIN,避免讀取也被拖進交易。
- `row_factory = sqlite3.Row`。
- 已執行 `PRAGMA journal_mode=WAL`、`PRAGMA foreign_keys=ON`(spec 2.1.1),
  另設 `busy_timeout` 讓並行寫入短暫等待而不是立刻 `database is locked`。

Migration(D3):以 `PRAGMA user_version` 記版本。`MIGRATIONS` 為 `(版本, SQL)` 依序排列;
`connect()` 對 `user_version` 小於 `SCHEMA_VERSION` 的庫,先以 `BEGIN IMMEDIATE` 取得寫鎖,
在鎖內重讀 `user_version`(多條連線同時第一次 connect 時,只有第一個拿到鎖的會建表,
其餘重讀後發現已是目標版本就直接 COMMIT),再逐一套用;所有 DDL 與 `user_version` 更新
在同一交易內,失敗時整個回滾、版本不前進。
空庫只建表與索引,不寫入任何資料列(spec 2)。庫的版本比程式新時丟 `SchemaVersionError`。
"""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from itertools import count
from pathlib import Path

BUSY_TIMEOUT_MS = 5000
_WAL_RETRY_SLEEP_S = 0.01


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
        _enable_wal(conn)
        conn.execute("PRAGMA foreign_keys=ON")
        _migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _enable_wal(conn: sqlite3.Connection) -> None:
    """設 WAL。新庫被多條連線同時第一次開啟時,切換 journal mode 可能直接回 SQLITE_BUSY
    (不經 busy handler),所以在 `BUSY_TIMEOUT_MS` 內短暫重試。只是等鎖,不涉業務時間。"""
    attempts = max(1, int(BUSY_TIMEOUT_MS / 1000 / _WAL_RETRY_SLEEP_S))
    for i in range(attempts):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_BUSY or i == attempts - 1:
                raise
            time.sleep(_WAL_RETRY_SLEEP_S)


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _check_version(current: int) -> None:
    if current > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"資料庫 schema 版本 {current} 比程式支援的 {SCHEMA_VERSION} 新"
        )


def _migrate(conn: sqlite3.Connection) -> None:
    current = _user_version(conn)
    _check_version(current)
    if current == SCHEMA_VERSION:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 鎖內重讀:可能已有別的連線在我們等鎖時完成 migration
        current = _user_version(conn)
        _check_version(current)
        for version, sql in MIGRATIONS:
            if version > current:
                _apply_migration(conn, version, sql)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _split_statements(sql: str) -> Iterator[str]:
    """依 `sqlite3.complete_statement` 把腳本切成單一敘述(註解與字串內的分號不會被誤切)。"""
    start = 0
    for i, ch in enumerate(sql):
        if ch == ";" and sqlite3.complete_statement(sql[start : i + 1]):
            yield sql[start : i + 1]
            start = i + 1
    rest = sql[start:]
    if rest.strip():
        yield rest


def _apply_migration(conn: sqlite3.Connection, version: int, sql: str) -> None:
    """在呼叫端已開啟的交易內執行一個 migration。不用 `executescript`:它會先 COMMIT
    進行中的交易,等於放掉 `_migrate` 取得的寫鎖。"""
    for stmt in _split_statements(sql):
        conn.execute(stmt)
    conn.execute(f"PRAGMA user_version = {int(version)}")


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
            # SQLite 遇到部分錯誤(如 SQLITE_FULL、中斷)會自動回滾整個外層交易,SAVEPOINT 也隨之消失;
            # 這時再 ROLLBACK TO 會丟出新的錯誤蓋掉原例外,所以只在交易仍在時回滾本層
            if conn.in_transaction:
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

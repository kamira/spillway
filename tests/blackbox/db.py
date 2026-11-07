"""唯讀資料庫存取(CHG-20261107-03 T2;決策 3、8 ⑤;AM-2)。

資料庫 schema 與資料目錄配置是跨語言契約(sdlc_docs/structure/data.md);黑箱測試只可**唯讀**開啟資料庫
檔案,所有寫入一律經 `/api` 或 `/__test__/*`。例外(AM-2):遷移類案例可在 serve 啟動**之前**以 sqlite3
從 SQL 檔建立測試資料庫(`build_db`)。

- `DB_NAME`:資料目錄內的資料庫檔名 `spillway.db`;`db_path(home_or_file)`。
- `read_db(home_or_path)`:context manager(資料目錄或資料庫檔案),`sqlite3` URI `file:…?mode=ro` 連線(寫入一律
  `sqlite3.OperationalError: attempt to write a readonly database`);離開時關閉。
- `snapshot(conn) -> dict[表名, list[tuple]]`:全表內容(每表的列排序後),比對「不寫入」用。
- `schema_snapshot(conn) -> dict`:正規化後的 schema(決策 3 / AM-2 的契約對象):
  `user_version`、`journal_mode`、`sqlite_master`(type、name、tbl_name、去註解並合併空白後的 sql;
  依 type、name 排序)、每表的 `table_info`、`index_list`(依 name 排序)、`index_info`、
  `foreign_key_list`。JSON 可序列化(tuple 一律轉成 list)。
- `setting(conn, key) -> str | None`:`settings` 表的值;存取 token 雜湊的鍵名為字面值
  `ACCESS_TOKEN_KEY = "access_token_sha256"`(data.md「settings 鍵」;AM-9)。
- `build_db(path, *scripts, user_version=None)`:serve 啟動前以 sqlite3 依序執行 SQL 腳本建立測試資料庫
  (例如 v1 舊庫);`user_version` 給了就設定。不在 serve 執行中使用。
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

__all__ = ["ACCESS_TOKEN_KEY", "DB_NAME", "build_db", "db_path", "read_db", "schema_snapshot", "setting",
           "snapshot"]

DB_NAME = "spillway.db"
ACCESS_TOKEN_KEY = "access_token_sha256"


def db_path(home: Path | str) -> Path:
    """資料目錄 → 其中的 `spillway.db`;已是檔案(或副檔名為 `.db`)時原樣回傳(例如 `.v1.bak` 備份)。"""
    p = Path(home)
    return p if p.is_file() or p.suffix == ".db" else p / DB_NAME


@contextmanager
def read_db(home_or_path: Path | str) -> Iterator[sqlite3.Connection]:
    path = db_path(home_or_path)
    if not path.is_file():
        raise FileNotFoundError(f"資料庫不存在:{path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        yield conn
    finally:
        conn.close()


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    return {t: sorted((tuple(r) for r in conn.execute(f'SELECT * FROM "{t}"')), key=repr) for t in _tables(conn)}


def _norm_sql(sql: str | None) -> str | None:
    if sql is None:
        return None
    no_comments = re.sub(r"--[^\n]*", "", sql)
    return re.sub(r"\s+", " ", no_comments).strip()


def _rows(conn: sqlite3.Connection, sql: str) -> list[list[Any]]:
    return [list(r) for r in conn.execute(sql)]


def schema_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    master = sorted(
        ([t, n, tbl, _norm_sql(s)] for t, n, tbl, s in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_stat%'")),
        key=lambda r: (r[0], r[1]))
    tables: dict[str, Any] = {}
    for t in _tables(conn):
        indexes = sorted(_rows(conn, f'PRAGMA index_list("{t}")'), key=lambda r: r[1])
        tables[t] = {
            "table_info": _rows(conn, f'PRAGMA table_info("{t}")'),
            # seq 只是 PRAGMA 的列序號,不屬於契約
            "index_list": [r[1:] for r in indexes],
            "index_info": {r[1]: _rows(conn, f'PRAGMA index_info("{r[1]}")') for r in indexes},
            "foreign_key_list": _rows(conn, f'PRAGMA foreign_key_list("{t}")'),
        }
    return {
        "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
        "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        "sqlite_master": master,
        "tables": tables,
    }


def setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def build_db(path: Path | str, *scripts: str, user_version: int | None = None) -> Path:
    p = db_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        for script in scripts:
            conn.executescript(script)
        if user_version is not None:
            conn.execute(f"PRAGMA user_version = {int(user_version)}")
        conn.commit()
    finally:
        conn.close()
    return p

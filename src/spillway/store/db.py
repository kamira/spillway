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

schema v2(CHG-20261101-03 決策 3、4、5):冷卻移到 `cooldowns` 表,`budgets` 移除 `exhausted_until`。
已有資料的舊版庫(`1 ≤ user_version < SCHEMA_VERSION`)升級前另有前置條件:
1. 取得資料目錄鎖檔 `spillway.lock`(`fcntl.flock` 非阻塞;同一行程內可重入,serve 啟動時由
   `acquire_data_dir_lock()` 取得並持有到行程結束)。取不到丟 `DataDirLockedError`,不 migrate。
2. 在 `BEGIN IMMEDIATE` 之前以 SQLite backup API 複製為 `spillway.db.v1.bak`(已存在時加 UTC 時間戳記
   後綴、不覆寫)。備份失敗:刪除殘檔、不 migrate,丟 `MigrationError`。
3. 備份前先做唯讀 `PRAGMA foreign_key_check`:舊庫已有違反時不備份、不升級,丟 `MigrationError`。
4. `BEGIN IMMEDIATE` 後重讀 `user_version`,再套用;完成後 `PRAGMA foreign_key_check` 必須為空。
   失敗回滾(原檔不變),丟 `MigrationError`(訊息含備份位置)。
空庫(`user_version = 0`)不需要鎖與備份,直接在一個交易內套用 v1、v2。

連線來源(CHG-20261101-02 決策 5):`ConnSource` 為 `sqlite3.Connection` 或回傳連線的零參數
callable;`use_conn(source)` 取得一條連線供 `with` 區塊使用,**一律不關閉**——傳入連線時直接交回,
傳入 callable 時呼叫一次並交回。關閉責任只在開啟者(Router 的 `close_conn`、
`api.deps.request_conn`、`RetentionScheduler`)。
"""
from __future__ import annotations

import fcntl
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib import resources
from itertools import count
from pathlib import Path

from spillway.clock import Clock, SystemClock

BUSY_TIMEOUT_MS = 5000
_WAL_RETRY_SLEEP_S = 0.01


ConnSource = sqlite3.Connection | Callable[[], sqlite3.Connection]


class SchemaVersionError(RuntimeError):
    """資料庫的 schema 版本比本程式支援的新。"""


def _schema_v1() -> str:
    return resources.files("spillway.store").joinpath("schema.sql").read_text(encoding="utf-8")


# v1 → v2(決策 3、4):建 cooldowns;把 budgets.exhausted_until 非 NULL 的列搬過去,reason 取該成員
# 帳本最近一筆 quota_exhausted / rate_limited(找不到時 quota_exhausted);DROP COLUMN。
# 不刪除任何 budgets 列(含 CHG-20261030-02 決策 59 的 type=none、window=none 暫行列)。
_SCHEMA_V2 = """
CREATE TABLE cooldowns (
  deployment_id   TEXT PRIMARY KEY REFERENCES deployments(id) ON DELETE CASCADE,
  exhausted_until TEXT NOT NULL,    -- 上游回報用完或限流時設定
  reason          TEXT NOT NULL CHECK (reason IN ('quota_exhausted','rate_limited'))
);

INSERT INTO cooldowns (deployment_id, exhausted_until, reason)
SELECT b.deployment_id, b.exhausted_until,
       COALESCE((SELECT l.status FROM usage_ledger l
                 WHERE l.deployment_id = b.deployment_id
                   AND l.status IN ('quota_exhausted','rate_limited')
                 ORDER BY l.ts DESC, l.id DESC LIMIT 1), 'quota_exhausted')
FROM budgets b
WHERE b.exhausted_until IS NOT NULL;

ALTER TABLE budgets DROP COLUMN exhausted_until;
"""

MIGRATIONS: tuple[tuple[int, str], ...] = ((1, _schema_v1()), (2, _SCHEMA_V2))
SCHEMA_VERSION = MIGRATIONS[-1][0]

LOCK_FILENAME = "spillway.lock"
BACKUP_SUFFIX = ".v{version}.bak"   # spillway.db → spillway.db.v1.bak


class DataDirLockedError(RuntimeError):
    """資料目錄鎖檔 `spillway.lock` 已被其他行程持有(通常是另一個 serve 正在執行)。"""


class MigrationError(RuntimeError):
    """舊版庫升級失敗(備份失敗、套用失敗或外鍵檢查不為空)。資料庫維持原版本。
    `backup_path` 為已建立的備份;備份本身失敗時為 None(殘檔已刪除)。"""

    def __init__(self, message: str, backup_path: Path | None = None) -> None:
        super().__init__(message)
        self.backup_path = backup_path


def connect(path: str | Path, *, clock: Clock | None = None) -> sqlite3.Connection:
    """開啟(必要時建立)資料庫並套用尚未執行的 migration。

    `clock` 只用在備份檔已存在時產生時間戳記後綴(預設 `SystemClock`)。"""
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        _enable_wal(conn)
        conn.execute("PRAGMA foreign_keys=ON")
        _migrate(conn, Path(path), clock)
    except BaseException:
        conn.close()
        raise
    return conn


# ---------------------------------------------------------------- 資料目錄鎖(決策 5 ②)


class _HeldLock:
    __slots__ = ("fd", "count")

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.count = 1


_held_locks: dict[Path, _HeldLock] = {}
_held_guard = threading.Lock()


def _lock_key(data_dir: str | Path) -> Path:
    return Path(data_dir).resolve()


def acquire_data_dir_lock(data_dir: str | Path) -> Path:
    """取得 `<data_dir>/spillway.lock` 的排他鎖(非阻塞)並持有,回傳鎖檔路徑。

    同一行程內可重入(計數);別的行程(或同行程另開的檔案描述)持有時丟 `DataDirLockedError`。
    鎖隨行程結束自動釋放;要提早釋放就呼叫同樣次數的 `release_data_dir_lock()`。"""
    key = _lock_key(data_dir)
    lock_path = key / LOCK_FILENAME
    with _held_guard:
        held = _held_locks.get(key)
        if held is not None:
            held.count += 1
            return lock_path
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise DataDirLockedError(
                f"資料目錄已被其他 spillway 行程使用(鎖檔 {lock_path}),可能已有 serve 在執行"
            ) from None
        _held_locks[key] = _HeldLock(fd)
    return lock_path


def release_data_dir_lock(data_dir: str | Path) -> None:
    """釋放一次 `acquire_data_dir_lock()`;計數歸零時解鎖並關閉鎖檔。未持有時不做事。"""
    key = _lock_key(data_dir)
    with _held_guard:
        held = _held_locks.get(key)
        if held is None:
            return
        held.count -= 1
        if held.count > 0:
            return
        del _held_locks[key]
        try:
            fcntl.flock(held.fd, fcntl.LOCK_UN)
        finally:
            os.close(held.fd)


@contextmanager
def data_dir_lock(data_dir: str | Path) -> Iterator[Path]:
    """`acquire_data_dir_lock()` 的 with 版本:離開區塊時釋放這一次。"""
    lock_path = acquire_data_dir_lock(data_dir)
    try:
        yield lock_path
    finally:
        release_data_dir_lock(data_dir)


# 同一行程內多執行緒同時升級同一個舊版庫時,只讓一個做備份與 migration
_upgrade_guards: dict[Path, threading.Lock] = {}


def _upgrade_guard(path: Path) -> threading.Lock:
    key = path.resolve()
    with _held_guard:
        return _upgrade_guards.setdefault(key, threading.Lock())


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


def _migrate(conn: sqlite3.Connection, path: Path, clock: Clock | None) -> None:
    while True:
        current = _user_version(conn)
        _check_version(current)
        if current == SCHEMA_VERSION:
            return
        if current == 0:
            # 空庫:不需要鎖與備份。等寫鎖期間別的連線可能已建好舊版 schema → 改走升級路徑
            if _migrate_tx(conn, from_empty_only=True):
                return
            continue
        _upgrade(conn, path, clock)
        return


def _upgrade(conn: sqlite3.Connection, path: Path, clock: Clock | None) -> None:
    """已有資料的舊版庫:鎖 → 備份 → 交易內 migration(決策 5 ②–④)。"""
    with _upgrade_guard(path), data_dir_lock(path.parent):
        current = _user_version(conn)   # 鎖內重讀:同行程的其他執行緒可能已升級完成
        _check_version(current)
        if current == SCHEMA_VERSION:
            return
        # 唯讀前置檢查:舊庫本身已有外鍵違反時不備份、不升級(否則必然在交易末端失敗)
        problems = _foreign_key_problems(conn)
        if problems:
            raise MigrationError(
                f"資料庫 schema v{current} 已有外鍵違反({problems}),未備份、未升級"
                f"(schema 仍為 v{current});請先修正資料。{_STOP_OLD_SERVE_HINT}"
            )
        backup = _backup(conn, path, current, clock)
        try:
            _migrate_tx(conn, from_empty_only=False)
        except Exception as exc:   # BaseException(KeyboardInterrupt 等)回滾後原樣往外丟
            raise MigrationError(
                f"資料庫 schema v{current} → v{SCHEMA_VERSION} 升級失敗,已回滾、原檔不變;"
                f"升級前備份:{backup}({type(exc).__name__}: {exc})。{_STOP_OLD_SERVE_HINT}",
                backup,
            ) from exc


_STOP_OLD_SERVE_HINT = "重試前請先停止舊版 serve(舊版程式不使用 spillway.lock)"


def _foreign_key_problems(conn: sqlite3.Connection) -> str:
    """`PRAGMA foreign_key_check` 的違反摘要:「子表→父表 N 列」以頓號相接;沒有違反時為空字串。"""
    counts: dict[tuple[str, str], int] = {}
    for row in conn.execute("PRAGMA foreign_key_check").fetchall():
        key = (row[0], row[2])   # (table, rowid, parent, fkid)
        counts[key] = counts.get(key, 0) + 1
    return "、".join(f"{child}→{parent} {n} 列" for (child, parent), n in sorted(counts.items()))


def _migrate_tx(conn: sqlite3.Connection, *, from_empty_only: bool) -> bool:
    """在一個 `BEGIN IMMEDIATE` 交易內套用尚未執行的 migration,再檢查外鍵。

    `from_empty_only` 為真時,鎖內重讀發現已是需要備份的舊版庫(0 < 版本 < 目標),就回滾並回傳
    False,交由呼叫端改走升級路徑。其餘情況回傳 True。失敗時回滾並重新丟出。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 鎖內重讀:可能已有別的連線在我們等鎖時完成 migration
        current = _user_version(conn)
        _check_version(current)
        if from_empty_only and 0 < current < SCHEMA_VERSION:
            conn.execute("ROLLBACK")
            return False
        for version, sql in MIGRATIONS:
            if version > current:
                _apply_migration(conn, version, sql)
        if current < SCHEMA_VERSION:
            problems = _foreign_key_problems(conn)
            if problems:
                raise sqlite3.IntegrityError(f"foreign_key_check 不為空:{problems}")
        conn.execute("COMMIT")
        return True
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _backup_target(path: Path, version: int, clock: Clock | None) -> Path:
    """`spillway.db.v1.bak`;已存在時加 UTC 時間戳記後綴(同一秒再加序號),一律不覆寫。"""
    base = path.with_name(path.name + BACKUP_SUFFIX.format(version=version))
    if not base.exists():
        return base
    stamp = (clock or SystemClock()).now().strftime("%Y%m%dT%H%M%SZ")
    target = base.with_name(f"{base.name}.{stamp}")
    n = 1
    while target.exists():
        n += 1
        target = base.with_name(f"{base.name}.{stamp}.{n}")
    return target


def _remove_backup_files(target: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            Path(str(target) + suffix).unlink()
        except FileNotFoundError:
            pass


def _backup(conn: sqlite3.Connection, path: Path, version: int, clock: Clock | None) -> Path:
    """以 SQLite backup API 複製整個庫(決策 5 ③);失敗時刪除殘檔並丟 `MigrationError`。"""
    target = _backup_target(path, version, clock)
    try:
        _copy_database(conn, target)
    except BaseException as exc:
        _remove_backup_files(target)
        if not isinstance(exc, Exception):
            raise
        raise MigrationError(
            f"升級前備份失敗,未升級(schema 仍為 v{version}):{type(exc).__name__}: {exc}"
        ) from exc
    return target


def _copy_database(conn: sqlite3.Connection, target: Path) -> None:
    """backup API 會一併讀入來源尚未 checkpoint 的 WAL 內容;複製後把備份改為 rollback journal
    (`journal_mode=DELETE`),讓 `.bak` 是單一自足的檔案(不需要 -wal、-shm)。"""
    dst = sqlite3.connect(str(target))
    try:
        conn.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
    finally:
        dst.close()


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


@contextmanager
def use_conn(source: ConnSource) -> Iterator[sqlite3.Connection]:
    """由 `ConnSource` 取得一條連線;離開時**不關閉**(含例外)。

    `source` 為連線時直接交回;為零參數 callable 時呼叫一次並交回其結果(例如 Router 綁定的
    `_conn`,回傳本次請求 contextvar 中的連線)。其他型別丟 `TypeError`。
    """
    # isinstance 必須在 callable() 之前:sqlite3.Connection 本身也有 `__call__`(callable() 為 True)
    if isinstance(source, sqlite3.Connection):
        yield source
    elif callable(source):
        yield source()
    else:
        raise TypeError(
            f"ConnSource 必須是 sqlite3.Connection 或零參數 callable,收到 {type(source).__name__}"
        )

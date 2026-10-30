"""T2 L1:schema v1 與 spec 2.3 一致、連線 PRAGMA、migration 冪等、外鍵串聯。"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from spillway.store import db as store_db
from spillway.store.db import SCHEMA_VERSION, connect

SPEC = Path(__file__).resolve().parents[3] / "docs" / "spec.md"

TABLES = (
    "deployments",
    "deployment_tags",
    "budgets",
    "groups",
    "routes",
    "route_targets",
    "usage_ledger",
    "usage_payloads",
    "settings",
)

# (name, type, notnull, dflt_value, pk) 依 spec 2.3 逐欄手抄;順序即欄位順序
EXPECTED_COLUMNS = {
    "deployments": [
        ("id", "TEXT", 0, None, 1),
        ("model_name", "TEXT", 1, None, 0),
        ("kind", "TEXT", 1, None, 0),
        ("provider", "TEXT", 1, None, 0),
        ("model", "TEXT", 1, None, 0),
        ("params", "TEXT", 1, "'{}'", 0),
        ("credential_ref", "TEXT", 0, None, 0),
        ("capabilities", "TEXT", 1, "'{}'", 0),
        ("enabled", "INTEGER", 1, "1", 0),
    ],
    "deployment_tags": [
        ("deployment_id", "TEXT", 1, None, 1),
        ("tag", "TEXT", 1, None, 2),
    ],
    "budgets": [
        ("deployment_id", "TEXT", 0, None, 1),
        ("type", "TEXT", 1, None, 0),
        ("limit_amount", "REAL", 0, None, 0),
        ("window", "TEXT", 1, "'monthly'", 0),
        ("reset_anchor", "INTEGER", 0, None, 0),
        ("exhausted_until", "TEXT", 0, None, 0),
    ],
    "groups": [
        ("name", "TEXT", 0, None, 1),
        ("selector", "TEXT", 1, None, 0),
    ],
    "routes": [
        ("name", "TEXT", 0, None, 1),
        ("target", "TEXT", 1, None, 0),
        ("mode", "TEXT", 1, "'chat'", 0),
        ("tags", "TEXT", 1, "'[]'", 0),
        ("record_context", "INTEGER", 0, None, 0),
        ("budget_precheck", "INTEGER", 1, "0", 0),
    ],
    "route_targets": [
        ("route_name", "TEXT", 1, None, 1),
        ("deployment_id", "TEXT", 1, None, 2),
        ("slot", "INTEGER", 1, None, 0),
        ("position", "INTEGER", 1, None, 0),
        ("effort", "TEXT", 0, None, 0),
        ("weight", "TEXT", 1, "'medium'", 0),
    ],
    "usage_ledger": [
        ("id", "INTEGER", 0, None, 1),
        ("ts", "TEXT", 1, None, 0),
        ("route_name", "TEXT", 0, None, 0),
        ("deployment_id", "TEXT", 0, None, 0),
        ("effort", "TEXT", 0, None, 0),
        ("tokens_in", "INTEGER", 0, None, 0),
        ("tokens_out", "INTEGER", 0, None, 0),
        ("cost_usd", "REAL", 0, None, 0),
        ("status", "TEXT", 1, None, 0),
        ("slot", "INTEGER", 0, None, 0),
        ("fallback_depth", "INTEGER", 1, "0", 0),
        ("latency_ms", "INTEGER", 0, None, 0),
        ("est_tokens_in", "INTEGER", 0, None, 0),
        ("est_tokens_out_p50", "INTEGER", 0, None, 0),
        ("est_tokens_out_p90", "INTEGER", 0, None, 0),
        ("est_cost_usd_p90", "REAL", 0, None, 0),
        ("error", "TEXT", 0, None, 0),
    ],
    "usage_payloads": [
        ("ledger_id", "INTEGER", 0, None, 1),
        ("ts", "TEXT", 1, None, 0),
        ("request_json", "TEXT", 1, None, 0),
        ("response_text", "TEXT", 0, None, 0),
        ("bytes", "INTEGER", 1, None, 0),
        ("truncated", "INTEGER", 1, "0", 0),
        ("redacted", "INTEGER", 1, "0", 0),
    ],
    "settings": [
        ("key", "TEXT", 0, None, 1),
        ("value", "TEXT", 1, None, 0),
    ],
}

EXPECTED_INDEXES = {
    "ix_ledger_dep_ts": ("usage_ledger", ["deployment_id", "ts"]),
    "ix_payloads_ts": ("usage_payloads", ["ts"]),
}


def _columns(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return [
        (r[1], r[2], r[3], r[4], r[5])
        for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
    ]


def _user_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def _explicit_indexes(conn: sqlite3.Connection) -> dict[str, tuple[str, list[str]]]:
    rows = conn.execute(
        "SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
    ).fetchall()
    out = {}
    for name, tbl in rows:
        cols = [r[2] for r in conn.execute(f"PRAGMA index_info({name})").fetchall()]
        out[name] = (tbl, cols)
    return out


def _normalized_sql(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"
    ).fetchall()
    out = {}
    for name, sql in rows:
        sql = re.sub(r"--[^\n]*", "", sql)
        out[name] = re.sub(r"\s+", " ", sql).strip()
    return out


def _spec_ddl() -> str:
    text = SPEC.read_text(encoding="utf-8")
    section = text.split("### 2.3 DDL", 1)[1]
    return section.split("```sql", 1)[1].split("```", 1)[0]


@pytest.fixture
def fresh(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "fresh.db")
    yield conn
    conn.close()


def test_pragmas_on_new_file(fresh):
    assert fresh.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert fresh.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert fresh.execute("PRAGMA user_version").fetchone()[0] == 1
    assert SCHEMA_VERSION == 1


def test_connect_sets_row_factory(fresh):
    assert fresh.row_factory is sqlite3.Row


def test_nine_tables_match_spec_columns(fresh):
    assert _user_tables(fresh) == set(TABLES)
    assert len(TABLES) == 9
    for table in TABLES:
        assert _columns(fresh, table) == EXPECTED_COLUMNS[table], table


def test_two_indexes_match_spec(fresh):
    assert _explicit_indexes(fresh) == EXPECTED_INDEXES


def test_schema_identical_to_spec_ddl_block(fresh):
    """以 docs/spec.md 2.3 的 DDL 原文建一個記憶體庫,逐表比對欄位、外鍵、索引與 CHECK。"""
    ref = sqlite3.connect(":memory:")
    try:
        ref.executescript(_spec_ddl())
        assert _user_tables(ref) == _user_tables(fresh)
        for table in TABLES:
            assert _columns(ref, table) == _columns(fresh, table), table
            fk_ref = ref.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            fk_got = [tuple(r) for r in fresh.execute(f"PRAGMA foreign_key_list({table})")]
            assert fk_ref == fk_got, table
        assert _explicit_indexes(ref) == _explicit_indexes(fresh)
        # 去掉註解與空白後的 CREATE 敘述相同 → CHECK、DEFAULT、REFERENCES 子句都一致
        assert _normalized_sql(ref) == _normalized_sql(fresh)
    finally:
        ref.close()


def test_all_tables_empty_after_init(fresh):
    for table in TABLES:
        assert fresh.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_foreign_keys_and_cascade_declared(fresh):
    fks = {
        table: {(r[2], r[3], r[4], r[6]) for r in fresh.execute(f"PRAGMA foreign_key_list({table})")}
        for table in TABLES
    }
    assert fks["deployment_tags"] == {("deployments", "deployment_id", "id", "CASCADE")}
    assert fks["budgets"] == {("deployments", "deployment_id", "id", "CASCADE")}
    assert fks["route_targets"] == {
        ("routes", "route_name", "name", "CASCADE"),
        ("deployments", "deployment_id", "id", "CASCADE"),
    }
    assert fks["usage_payloads"] == {("usage_ledger", "ledger_id", "id", "CASCADE")}
    assert fks["usage_ledger"] == set()


def test_reconnect_does_not_rebuild(tmp_path):
    path = tmp_path / "again.db"
    c1 = connect(path)
    c1.execute("INSERT INTO settings(key, value) VALUES ('k', 'v')")
    c1.execute(
        "INSERT INTO deployments(id, model_name, kind, provider, model) "
        "VALUES ('d1', 'm', 'api', 'openai', 'gpt')"
    )
    c1.close()

    c2 = connect(path)
    try:
        assert c2.execute("PRAGMA user_version").fetchone()[0] == 1
        assert c2.execute("SELECT value FROM settings WHERE key='k'").fetchone()[0] == "v"
        assert c2.execute("SELECT COUNT(*) FROM deployments").fetchone()[0] == 1
        assert c2.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        c2.close()


def test_migration_not_reapplied_when_version_current(tmp_path, monkeypatch):
    path = tmp_path / "m.db"
    connect(path).close()
    calls = []
    original = store_db._apply_migration

    def spy(conn, version, sql):
        calls.append(version)
        return original(conn, version, sql)

    monkeypatch.setattr(store_db, "_apply_migration", spy)
    connect(path).close()
    assert calls == []


def test_newer_schema_version_is_rejected(tmp_path):
    path = tmp_path / "future.db"
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 99")
    raw.close()
    with pytest.raises(store_db.SchemaVersionError):
        connect(path)


def test_failed_migration_leaves_empty_database(tmp_path, monkeypatch):
    path = tmp_path / "broken.db"
    monkeypatch.setattr(
        store_db,
        "MIGRATIONS",
        ((1, "CREATE TABLE a (x INTEGER); CREATE TABLE a (x INTEGER);"),),
    )
    with pytest.raises(sqlite3.Error):
        connect(path)
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
        assert _user_tables(raw) == set()
    finally:
        raw.close()


def test_delete_deployment_cascades_but_keeps_ledger(fresh):
    c = fresh
    c.execute(
        "INSERT INTO deployments(id, model_name, kind, provider, model) "
        "VALUES ('d1', 'm', 'api', 'openai', 'gpt')"
    )
    c.execute("INSERT INTO deployment_tags VALUES ('d1', 'fast')")
    c.execute("INSERT INTO budgets(deployment_id, type, limit_amount) VALUES ('d1', 'usd', 10)")
    c.execute("INSERT INTO routes(name, target) VALUES ('r1', '{\"group\": \"g\"}')")
    c.execute(
        "INSERT INTO route_targets(route_name, deployment_id, slot, position) "
        "VALUES ('r1', 'd1', 1, 0)"
    )
    c.execute(
        "INSERT INTO usage_ledger(ts, route_name, deployment_id, status) "
        "VALUES ('2026-10-30T12:00:00Z', 'r1', 'd1', 'ok')"
    )
    c.execute("DELETE FROM deployments WHERE id = 'd1'")
    assert c.execute("SELECT COUNT(*) FROM deployment_tags").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM budgets").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM route_targets").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM routes").fetchone()[0] == 1
    assert c.execute("SELECT deployment_id FROM usage_ledger").fetchone()[0] == "d1"


def test_check_constraints_enforced(fresh):
    with pytest.raises(sqlite3.IntegrityError):
        fresh.execute(
            "INSERT INTO deployments(id, model_name, kind, provider, model) "
            "VALUES ('x', 'm', 'weird', 'p', 'm')"
        )


def test_db_fixture_is_migrated(db):
    assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert _user_tables(db) == set(TABLES)


def test_concurrent_first_connect_migrates_once(tmp_path):
    """M2:多條連線同時第一次 connect 同一個新庫,全部成功、只建一次表。"""
    import threading

    threads_n, rounds = 8, 20
    for r in range(rounds):
        path = tmp_path / f"race{r}.db"
        barrier = threading.Barrier(threads_n)
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait()
                connect(path).close()
            except BaseException as exc:  # noqa: BLE001 - 收集後統一斷言
                errors.append(exc)

        ts = [threading.Thread(target=worker) for _ in range(threads_n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert errors == [], f"round {r}: {errors!r}"
        raw = sqlite3.connect(path)
        try:
            assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 1
            assert _user_tables(raw) == set(TABLES)
        finally:
            raw.close()


def test_migration_waits_for_lock_and_rereads_version(tmp_path):
    """M2:等鎖期間別的連線已完成 migration 時,鎖內重讀版本後不再套用。"""
    path = tmp_path / "held.db"
    raw = sqlite3.connect(path, isolation_level=None)
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("BEGIN IMMEDIATE")
    for stmt in store_db._split_statements(store_db.MIGRATIONS[0][1]):
        raw.execute(stmt)
    raw.execute("PRAGMA user_version = 1")

    import threading

    result: list[object] = []

    def worker() -> None:
        try:
            c = connect(path)
            result.append(c.execute("PRAGMA user_version").fetchone()[0])
            c.close()
        except BaseException as exc:  # noqa: BLE001
            result.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join(0.3)
    assert t.is_alive()  # 卡在 BEGIN IMMEDIATE 等鎖
    raw.execute("COMMIT")
    t.join()
    raw.close()
    assert result == [1]


def test_split_statements_ignores_semicolons_in_comments_and_strings():
    sql = "-- a; b\nCREATE TABLE t (x TEXT DEFAULT ';');\n/* ; */ CREATE INDEX i ON t(x);\n"
    stmts = [s.strip() for s in store_db._split_statements(sql)]
    assert len(stmts) == 2
    assert stmts[0].endswith("DEFAULT ';');")
    assert stmts[1].endswith("CREATE INDEX i ON t(x);")

"""`repo.read_tx`(CHG-20261103-01 T1;決策 21 ④)。

L2:巢狀呼叫只開一個交易;在 `transaction()` 內呼叫不會結束外層交易(外層寫入在 COMMIT 後可見);
自行開的讀交易結束時 ROLLBACK;`read_snapshot` 改用它。
"""
from __future__ import annotations

import sqlite3

import pytest

from spillway.store import repo
from spillway.store.db import connect, transaction


def _traced(conn: sqlite3.Connection) -> list[str]:
    stmts: list[str] = []
    conn.set_trace_callback(lambda s: stmts.append(" ".join(s.split()).upper()))
    return stmts


def _tx_stmts(stmts: list[str]) -> list[str]:
    # 只看頂層交易的開始與結束;repo 寫入函式在交易內以 SAVEPOINT 巢狀,不列入
    return [s for s in stmts if s.split()[0] in ("BEGIN", "COMMIT", "ROLLBACK")]


def test_read_tx_opens_deferred_and_rolls_back(db):
    stmts = _traced(db)
    with repo.read_tx(db) as c:
        assert c is db
        assert db.in_transaction
        db.execute("SELECT COUNT(*) FROM deployments").fetchone()
    assert not db.in_transaction
    assert _tx_stmts(stmts) == ["BEGIN", "ROLLBACK"]


def test_read_tx_rolls_back_on_exception(db):
    stmts = _traced(db)
    with pytest.raises(RuntimeError):
        with repo.read_tx(db):
            raise RuntimeError("boom")
    assert not db.in_transaction
    assert _tx_stmts(stmts) == ["BEGIN", "ROLLBACK"]


def test_nested_read_tx_opens_one_transaction(db):
    stmts = _traced(db)
    with repo.read_tx(db):
        with repo.read_tx(db):
            assert db.in_transaction
        assert db.in_transaction
    assert not db.in_transaction
    assert _tx_stmts(stmts) == ["BEGIN", "ROLLBACK"]


def test_read_tx_inside_transaction_keeps_outer(tmp_path):
    path = tmp_path / "rt.db"
    conn = connect(path)
    other = connect(path)
    try:
        stmts = _traced(conn)
        with transaction(conn):
            repo.set_setting(conn, "record_context_default", "1")
            with repo.read_tx(conn):
                assert conn.in_transaction
            assert conn.in_transaction       # 外層交易沒有被結束
            repo.set_setting(conn, "context_retention_days", "7")
        assert _tx_stmts(stmts) == ["BEGIN IMMEDIATE", "COMMIT"]
        assert repo.get_setting(other, "record_context_default") == "1"
        assert repo.get_setting(other, "context_retention_days") == "7"
    finally:
        conn.close()
        other.close()


def test_read_snapshot_uses_read_tx(db):
    stmts = _traced(db)
    with repo.read_snapshot(db):
        db.execute("SELECT 1").fetchone()
    assert not db.in_transaction
    assert _tx_stmts(stmts) == ["BEGIN", "ROLLBACK"]

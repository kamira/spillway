"""`POST /api/settings/token/rotate`(CHG-20261101-03 T7;決策 14;spec 8.1.3)。

- 回應 `{"token": "..."}`(只此一次)、`Cache-Control: no-store`;資料庫只存 SHA-256 十六進位。
- token 明文不出現在 caplog(root DEBUG)、capfd、帳本、資料庫檔案(含 WAL)與資料目錄的任何檔案中。
- 不帶 body;多餘的 body 欄位不影響(端點不讀 body)。
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

import pytest

from spillway import settings
from spillway.store import repo

pytestmark = pytest.mark.anyio

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
ROTATE = "/api/settings/token/rotate"


def _files_containing(root: Path, needle: str) -> list[Path]:
    data = needle.encode()
    return [p for p in root.rglob("*") if p.is_file() and data in p.read_bytes()]


async def test_rotate_returns_token_once_and_stores_hash(client, db):
    assert settings.get_access_token_hash(db) is None   # 行程內 create_app 預設不產生(由 serve 產生)
    r = await client.post(ROTATE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert list(body) == ["token"]
    token = body["token"]
    assert TOKEN_RE.match(token)
    assert r.headers["cache-control"] == "no-store"
    assert settings.get_access_token_hash(db) == hashlib.sha256(token.encode()).hexdigest()
    rows = db.execute("SELECT key, value FROM settings").fetchall()
    assert [tuple(x) for x in rows] == [("access_token_sha256", settings.hash_access_token(token))]


async def test_rotate_again_replaces_hash(client, db):
    first = (await client.post(ROTATE)).json()["token"]
    second = (await client.post(ROTATE)).json()["token"]
    assert first != second
    assert settings.get_access_token_hash(db) == settings.hash_access_token(second)
    assert settings.get_access_token_hash(db) != settings.hash_access_token(first)


async def test_rotate_token_not_in_logs_ledger_or_files(client, db, db_path, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    capfd.readouterr()
    caplog.clear()
    r = await client.post(ROTATE)
    assert r.status_code == 200
    token = r.json()["token"]
    # 再打一個一般請求,確保若有請求日誌也已寫出
    assert (await client.get("/api/settings/context")).status_code == 200
    out, err = capfd.readouterr()
    assert token not in caplog.text
    assert token not in out and token not in err
    assert repo.count_ledger(db) == 0
    assert all(token not in repr(tuple(row)) for row in db.execute("SELECT * FROM usage_ledger"))
    assert _files_containing(db_path.parent, token) == []

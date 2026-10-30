"""`GET/PUT /api/settings/context`(spec 2.4、9.2.1;決策 24)(CHG-20261030-02 T14)。"""
from __future__ import annotations

import pytest

from spillway.store import repo

from .helpers import assert_error

pytestmark = pytest.mark.anyio


async def test_defaults_without_rows(client, db):
    r = await client.get("/api/settings/context")
    assert r.status_code == 200
    assert r.json() == {"record_context_default": True, "context_retention_days": 7,
                        "context_max_bytes": 262144}
    assert db.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0   # 讀取不寫列


async def test_put_partial_and_full(client, db):
    r = await client.put("/api/settings/context", json={"context_retention_days": 0})
    assert r.status_code == 200, r.text
    assert r.json() == {"record_context_default": True, "context_retention_days": 0,
                        "context_max_bytes": 262144}
    r = await client.put("/api/settings/context", json={
        "record_context_default": False, "context_retention_days": 30, "context_max_bytes": 1024})
    assert r.json() == {"record_context_default": False, "context_retention_days": 30,
                        "context_max_bytes": 1024}
    s = repo.get_context_settings(db)
    assert (s.record_default, s.retention_days, s.max_bytes) == (False, 30, 1024)
    assert (await client.get("/api/settings/context")).json() == r.json()


@pytest.mark.parametrize("body,path", [
    ({"context_retention_days": -1}, "body.context_retention_days"),
    ({"context_retention_days": 1.5}, "body.context_retention_days"),
    ({"context_max_bytes": 1023}, "body.context_max_bytes"),
    ({"context_max_bytes": "4096"}, "body.context_max_bytes"),
    ({"record_context_default": "yes"}, "body.record_context_default"),
    ({"record_context_default": 1}, "body.record_context_default"),
    ({"bind_host": "0.0.0.0"}, "body.bind_host"),
])
async def test_put_validation(client, db, body, path):
    msg = assert_error(await client.put("/api/settings/context", json=body), 422,
                       "validation_error")
    assert path in msg
    assert db.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0

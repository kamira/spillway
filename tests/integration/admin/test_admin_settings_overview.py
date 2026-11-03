"""`GET /api/settings` 設定總覽(CHG-20261102-02 T5;決策 10、19;決策審議 AM-3、AM-11)。"""
from __future__ import annotations

import importlib.metadata
from pathlib import Path

import pytest

import spillway
from spillway import settings as spillway_settings
from spillway.api.admin import settings as settings_api
from spillway.ledger import Attempt, LedgerWriter
from spillway.store import repo

from .helpers import local_member

pytestmark = pytest.mark.anyio

KEYS = {"port", "data_dir", "version", "token_issued", "first_run", "counts", "context"}


async def test_shape_on_empty_database(client, db_path):
    r = await client.get("/api/settings")
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == KEYS
    assert out["counts"] == {"deployments": 0, "groups": 0, "routes": 0, "ledger_ok": 0}
    assert out["context"] == {"record_context_default": True, "context_retention_days": 7,
                              "context_max_bytes": 262144, "payload_count": 0, "payload_bytes": 0}
    assert out["first_run"] is True
    assert out["token_issued"] is False
    assert out["port"] == client._transport.app.state.port == 8787
    assert out["data_dir"] == str(Path(db_path).parent)


async def test_first_run_false_after_member(client):
    r = await client.post("/api/deployments", json=local_member("m1"))
    assert r.status_code == 201, r.text
    out = (await client.get("/api/settings")).json()
    assert out["first_run"] is False
    assert out["counts"]["deployments"] == 1


async def test_first_run_false_with_route_only(client):
    r = await client.post("/api/groups", json={"name": "g", "selector": {}})
    assert r.status_code == 201, r.text
    out = (await client.get("/api/settings")).json()
    assert out["counts"]["groups"] == 1 and out["first_run"] is True
    r = await client.post("/api/routes", json={"name": "rt", "target": {"group": "g"}})
    assert r.status_code == 201, r.text
    out = (await client.get("/api/settings")).json()
    assert out["counts"]["routes"] == 1 and out["counts"]["deployments"] == 0
    assert out["first_run"] is False


async def test_token_issued_matches_settings_table(client, db):
    assert (await client.get("/api/settings")).json()["token_issued"] is False
    token = spillway_settings.rotate_access_token(db)
    r = await client.get("/api/settings")
    assert r.json()["token_issued"] is (spillway_settings.get_access_token_hash(db) is not None)
    assert r.json()["token_issued"] is True
    h = spillway_settings.get_access_token_hash(db)
    assert token not in r.text and h not in r.text
    assert "token" not in {k for k in r.json() if k != "token_issued"}


async def test_counts_and_payload_stats(client, db, clock):
    w = LedgerWriter(db)
    w.record(Attempt(ts=clock.now(), status="ok", route_name="rt", deployment_id="a",
                     request_text='[{"role": "user", "content": "hello"}]', response_text="hi"))
    w.record(Attempt(ts=clock.now(), status="error", deployment_id="a", error="x",
                     request_text='[{"role": "user", "content": "again"}]'))
    repo.set_setting(db, "context_retention_days", 30)
    out = (await client.get("/api/settings")).json()
    assert out["counts"]["ledger_ok"] == 1
    count, total = repo.payload_stats(db)
    assert count == 2 and total == db.execute(
        "SELECT SUM(bytes) FROM usage_payloads").fetchone()[0] > 0
    assert out["context"]["payload_count"] == 2
    assert out["context"]["payload_bytes"] == total
    assert out["context"]["context_retention_days"] == 30


def test_count_config_and_payload_stats_empty(db):
    assert repo.count_config(db) == {"deployments": 0, "groups": 0, "routes": 0, "ledger_ok": 0}
    assert repo.payload_stats(db) == (0, 0)


async def test_version_equals_metadata_or_dunder(client):
    try:
        expected = importlib.metadata.version("spillway")
    except importlib.metadata.PackageNotFoundError:
        expected = spillway.__version__
    assert (await client.get("/api/settings")).json()["version"] == expected


def test_version_falls_back_to_dunder(monkeypatch):
    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    assert settings_api._version() == spillway.__version__


async def test_port_and_data_dir_follow_app_state(make_app, make_client, tmp_path):
    app = make_app()
    app.state.port = 9123
    async with make_client(app) as c:
        out = (await c.get("/api/settings")).json()
    assert out["port"] == 9123
    assert out["data_dir"] == str(Path(app.state.db_path).parent)


async def test_does_not_call_settings_data_dir(client, monkeypatch):
    def boom():
        raise AssertionError("不可呼叫 settings.data_dir()")

    monkeypatch.setattr(spillway_settings, "data_dir", boom)
    assert (await client.get("/api/settings")).status_code == 200

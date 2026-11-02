"""`GET /api/config/export`(CHG-20261102-02 T3;決策 5、19;spec 3、9.1.4)。匯入部分由 T4 加入本檔。

L1:舊列 `params`/`capabilities` 含憑證鍵名或疑似金鑰值 → 422 `validation_error`,只回位置、不回顯值。
L2:Content-Type、Content-Disposition(UTC 檔名,時間取自注入的 clock)、Cache-Control、nosniff;
內容等於 `configio.dump`(黃金檔);不含假金鑰值與 token;Host/Origin 照常適用;匯出不寫資料庫。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from spillway import configio
from spillway.store import repo

from .helpers import KEY, api_member, assert_error

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "unit"))
from test_configio_dump import GOLDEN, seed_golden  # noqa: E402

pytestmark = pytest.mark.anyio

EXPORT = "/api/config/export"


def _snapshot(db) -> list[str]:
    return list(db.iterdump())


async def test_export_headers_and_body(client, db, clock):
    seed_golden(db)
    r = await client.get(EXPORT)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/yaml; charset=utf-8"
    assert r.headers["content-disposition"] == (
        'attachment; filename="spillway-config-20261030T120000Z.yaml"')
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.content == GOLDEN.read_bytes()
    assert r.text == configio.dump(db)


async def test_filename_follows_clock(client, clock):
    clock.advance(days=3, hours=1, minutes=2, seconds=3)
    r = await client.get(EXPORT)
    assert r.status_code == 200
    assert 'filename="spillway-config-20261102T130203Z.yaml"' in r.headers["content-disposition"]


async def test_empty_export(client):
    r = await client.get(EXPORT)
    assert r.status_code == 200
    assert r.text == "version: 1\nsettings: {}\ndeployments: []\ngroups: {}\nroutes: {}\n"


async def test_export_has_no_secret_or_token(client, db):
    r = await client.post("/api/deployments", json=api_member("api-x"))
    assert r.status_code == 201, r.text
    token = (await client.post("/api/settings/token/rotate")).json()["token"]
    token_hash = repo.get_setting(db, "access_token_sha256")
    r = await client.get(EXPORT)
    assert r.status_code == 200
    assert "credential_ref: env:SPILLWAY_TEST_KEY" in r.text
    for needle in (KEY, token, token_hash):
        assert needle not in r.text


async def test_export_does_not_write(client, db):
    seed_golden(db)
    before = _snapshot(db)
    assert (await client.get(EXPORT)).status_code == 200
    assert _snapshot(db) == before


@pytest.mark.parametrize("column,value,path", [
    ("params", {"access_token": "x"}, "deployments[legacy].params.access_token"),
    ("params", {"nested": {"api-key": "x"}}, "deployments[legacy].params.nested.api-key"),
    ("params", {"note": KEY}, "deployments[legacy].params.note"),
    ("capabilities", {"ctx": 1, "secret": "x"}, "deployments[legacy].capabilities.secret"),
])
async def test_legacy_secret_rejected(client, db, column, value, path):
    db.execute(
        "INSERT INTO deployments (id, model_name, kind, provider, model, params, capabilities) "
        "VALUES ('legacy', 'm', 'api', 'openai', 'gpt', ?, ?)",
        (json.dumps(value if column == "params" else {}),
         json.dumps(value if column == "capabilities" else {})),
    )
    r = await client.get(EXPORT)
    msg = assert_error(r, 422, "validation_error")
    assert path in msg
    assert "x" not in r.text          # 不回顯值(訊息與錯誤碼都不含字母 x)
    assert KEY not in r.text
    assert "content-disposition" not in r.headers


async def test_legacy_hits_all_listed(client, db):
    for dep_id in ("old-a", "old-b"):
        db.execute(
            "INSERT INTO deployments (id, model_name, kind, provider, model, params) "
            "VALUES (?, 'm', 'api', 'openai', 'gpt', ?)", (dep_id, json.dumps({"password": "x"})))
    msg = assert_error(await client.get(EXPORT), 422, "validation_error")
    assert "deployments[old-a].params.password" in msg
    assert "deployments[old-b].params.password" in msg


@pytest.mark.parametrize("headers", [
    {"Origin": "http://evil.example"},
    {"Host": "evil.example:8787"},
])
async def test_host_origin_apply(client, db, headers):
    seed_golden(db)
    r = await client.get(EXPORT, headers=headers)
    assert r.status_code == 403, r.text
    assert "deployments" not in r.text


async def test_same_origin_allowed(client):
    r = await client.get(EXPORT, headers={"Origin": "http://127.0.0.1:8787"})
    assert r.status_code == 200

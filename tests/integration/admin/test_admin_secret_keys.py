"""憑證鍵名正規化比對與 `params.signals` 寫入驗證(CHG-20261101-02 T7;決策 1、4)。"""
from __future__ import annotations

import pytest

from spillway.api.admin import secrets
from spillway.store import repo

from .helpers import KEY, api_member, assert_error

pytestmark = pytest.mark.anyio

VALUE = "abc"


# ---------------------------------------------------------------- L1 正規化判定


@pytest.mark.parametrize("name", [
    "X-Api-Key", "client_secret", "openai_api_key", "Refresh-Token", "db.password", "secret_key",
    "private_key", "aws_access_key", "api_token", "session_token", "x-auth-token", "passphrase",
    "api_keys",
    # 原 8 個鍵名(U9)照舊
    "api_key", "apikey", "key", "token", "access_token", "secret", "password", "authorization",
    # 其他完全比對項與大小寫、空白變體
    "Bearer", "credentials", "Credential", "passwd", "x api key", "API.KEYS",
])
def test_credential_key_names(name):
    assert secrets.is_credential_key(name)


@pytest.mark.parametrize("name", [
    "max_tokens", "max_token", "keyword", "tokenizer", "monkey", "cache_key", "quota",
    "rate_limit", "http_status", "error_code", "exit_code", "exit_code_not", "match", "equals",
    "api_base", "timeout", "token_budget", "keys", "secret_sauce", "signals",
])
def test_non_credential_key_names(name):
    assert not secrets.is_credential_key(name)


# ---------------------------------------------------------------- L2 POST / PUT


BLOCKED = [
    ({"x-api-key": VALUE}, "params.x-api-key"),
    ({"headers": {"Client-Secret": VALUE}}, "params.headers.Client-Secret"),
    ({"list": [{"secret_key": VALUE}]}, "params.list.0.secret_key"),
    ({"signals": {"quota": [{"equals": {"api_key": VALUE}}]}},
     "params.signals.quota.0.equals.api_key"),
]


@pytest.mark.parametrize("params,path", BLOCKED)
async def test_post_variant_credential_keys_rejected(client, db, params, path):
    r = await client.post("/api/deployments", json=api_member("a", params=params))
    msg = assert_error(r, 422, "validation_error")
    assert path in msg
    assert VALUE not in msg
    assert repo.load_deployment(db, "a") is None


@pytest.mark.parametrize("params,path", BLOCKED)
async def test_put_variant_credential_keys_rejected(client, db, params, path):
    assert (await client.post("/api/deployments", json=api_member("a"))).status_code == 201
    r = await client.put("/api/deployments/a", json=api_member("a", params=params))
    msg = assert_error(r, 422, "validation_error")
    assert path in msg
    assert VALUE not in msg
    assert repo.load_deployment(db, "a").params == {}


async def test_capabilities_extra_variant_key_rejected(client, db):
    caps = {"ctx": 1000, "extra": {"x-auth-token": VALUE}}
    r = await client.post("/api/deployments", json=api_member("a", capabilities=caps))
    msg = assert_error(r, 422, "validation_error")
    assert "capabilities.extra.x-auth-token" in msg
    assert VALUE not in msg
    assert repo.load_deployment(db, "a") is None


VALID_SIGNALS = {"rate_limit": [{"http_status": 429}],
                 "quota": [{"error_code": ["insufficient_quota"],
                            "match": {"message": "credit|limit"}}]}


async def test_valid_signals_written_and_read_back(client, db):
    params = {"api_base": "http://127.0.0.1:9/v1", "signals": VALID_SIGNALS}
    r = await client.post("/api/deployments", json=api_member("a", params=params))
    assert r.status_code == 201, r.text
    assert r.json()["params"]["signals"] == VALID_SIGNALS
    assert (await client.get("/api/deployments/a")).json()["params"]["signals"] == VALID_SIGNALS
    assert repo.load_deployment(db, "a").params["signals"] == VALID_SIGNALS


async def test_put_valid_signals(client, db):
    assert (await client.post("/api/deployments", json=api_member("a"))).status_code == 201
    r = await client.put("/api/deployments/a",
                         json=api_member("a", params={"signals": VALID_SIGNALS}))
    assert r.status_code == 200, r.text
    assert repo.load_deployment(db, "a").params["signals"] == VALID_SIGNALS


BAD_SIGNALS = [
    ({"no_such_kind": [{"http_status": 429}]}, "params.signals.no_such_kind"),
    ({"quota": {"http_status": 402}}, "params.signals.quota"),
    ({"quota": [{"http_status": "zq9badvalue"}]}, "params.signals.quota[0].http_status"),
    ({"rate_limit": [{"nope": 1}]}, "params.signals.rate_limit[0].nope"),
    (["zq9badvalue"], "params.signals"),
]


@pytest.mark.parametrize("signals,path", BAD_SIGNALS)
async def test_post_invalid_signals_rejected(client, db, signals, path):
    r = await client.post("/api/deployments", json=api_member("a", params={"signals": signals}))
    msg = assert_error(r, 422, "validation_error")
    assert path in msg
    assert "zq9badvalue" not in msg
    assert repo.load_deployment(db, "a") is None


@pytest.mark.parametrize("signals,path", BAD_SIGNALS)
async def test_put_invalid_signals_rejected(client, db, signals, path):
    assert (await client.post("/api/deployments", json=api_member("a"))).status_code == 201
    r = await client.put("/api/deployments/a", json=api_member("a", params={"signals": signals}))
    msg = assert_error(r, 422, "validation_error")
    assert path in msg
    assert "zq9badvalue" not in msg
    assert repo.load_deployment(db, "a").params == {}


# 正規式不合法:訊息含欄位路徑與 re.error 文字,re.error 可能帶出正規式片段(如群組名),不保證不回顯;
# 疑似金鑰的字串在值樣式掃描就被擋下並遮罩。


async def test_invalid_regex_message_carries_re_error_text(client, db):
    r = await client.post("/api/deployments", json=api_member(
        "a", params={"signals": {"quota": [{"match": {"message": "(?P=grp)"}}]}}))
    msg = assert_error(r, 422, "validation_error")
    assert "params.signals.quota[0].match.message" in msg
    assert "正規式不合法" in msg
    assert repo.load_deployment(db, "a") is None


async def test_credential_value_inside_regex_is_masked(client, db):
    r = await client.post("/api/deployments", json=api_member(
        "a", params={"signals": {"quota": [{"match": {"message": f"(?P={KEY})"}}]}}))
    assert_error(r, 422, "validation_error")
    assert KEY not in r.text
    assert repo.load_deployment(db, "a") is None


async def test_null_signals_accepted_on_post_and_put(client, db):
    r = await client.post("/api/deployments", json=api_member("a", params={"signals": None}))
    assert r.status_code == 201, r.text
    assert repo.load_deployment(db, "a").params == {"signals": None}
    r = await client.put("/api/deployments/a", json=api_member("a", params={"signals": None}))
    assert r.status_code == 200, r.text
    assert repo.load_deployment(db, "a").params == {"signals": None}

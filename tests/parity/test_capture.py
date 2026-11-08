"""上游請求擷取形狀的契約測試(CHG-20261108-01 T3;AM-7;spec 15.3;契約清單「上游請求擷取形狀」)。"""
from __future__ import annotations

import json

import httpx
import pytest

from tests.fakes.api_upstream.server import FakeApiServer

KEY = "sk-test-capture-value-01"
QUERY_VALUE = "query-secret-value"


@pytest.fixture
def fake():
    server = FakeApiServer().start()
    yield server
    server.stop()


def _client(fake: FakeApiServer) -> httpx.Client:
    return httpx.Client(base_url=fake.base_url, trust_env=False)


def test_records_names_only_never_values(fake):
    with _client(fake) as c:
        c.post("/v1/chat/completions?b=" + QUERY_VALUE + "&a=1&a=2", json={"model": "m"},
               headers={"Authorization": f"Bearer {KEY}", "X-Custom-Thing": "header-value"})
    rec = fake.requests[0]
    assert "x-custom-thing" in rec.header_names and "authorization" in rec.header_names
    assert list(rec.header_names) == sorted(rec.header_names) and all(n == n.lower() for n in rec.header_names)
    assert rec.query_names == ("a", "b")
    assert rec.auth_headers == ("authorization",)
    dumped = json.dumps(rec.as_dict())
    assert KEY not in dumped and QUERY_VALUE not in dumped and "header-value" not in dumped


def test_x_api_key_is_an_auth_header_name(fake):
    with _client(fake) as c:
        c.post("/v1/messages", json={"model": "m"}, headers={"x-api-key": KEY})
    rec = fake.requests[0]
    assert rec.auth_headers == ("x-api-key",)
    assert KEY not in json.dumps(rec.as_dict())


def test_no_auth_headers_is_empty(fake):
    with _client(fake) as c:
        c.post("/v1/chat/completions", json={"model": "m"})
    assert fake.requests[0].auth_headers == ()


@pytest.mark.parametrize("payload", [[1, 2], "text", 3, None])
def test_non_dict_json_is_recorded(fake, payload):
    with _client(fake) as c:
        c.post("/v1/chat/completions", content=json.dumps(payload).encode(), headers={"content-type": "application/json"})
    rec = fake.requests[0]
    assert rec.has_json is True and rec.json_body == payload
    assert rec.body is None    # 既有欄位語意不變


def test_non_json_records_length_only(fake):
    with _client(fake) as c:
        c.post("/v1/chat/completions", content=b"not json at all")
    rec = fake.requests[0]
    assert rec.has_json is False and rec.json_body is None and rec.body_length == len(b"not json at all")


@pytest.mark.parametrize("raw", [b'{"a": NaN}', b'{"a": Infinity}', b'{"a": -Infinity}'])
def test_nan_and_infinity_are_not_json(fake, raw):
    with _client(fake) as c:
        c.post("/v1/chat/completions", content=raw)
    assert fake.requests[0].has_json is False


def test_get_models_has_the_capture_shape(fake):
    with _client(fake) as c:
        c.get("/v1/models?x=1", headers={"Authorization": f"Bearer {KEY}"})
    rec = fake.requests[0]
    assert rec.method == "GET" and rec.query_names == ("x",) and rec.auth_headers == ("authorization",)
    assert rec.has_json is False and rec.body_length == 0


@pytest.mark.parametrize("method,path,status", [("POST", "/v1/responses", 404), ("GET", "/v1/chat/completions", 404),
                                                  ("DELETE", "/v1/models", 404), ("GET", "/", 200)])
def test_unrouted_requests_are_recorded(fake, method, path, status):
    with _client(fake) as c:
        r = c.request(method, f"{path}?q={QUERY_VALUE}", headers={"Authorization": f"Bearer {KEY}", "X-Custom-Thing": "header-value"})
    assert r.status_code == status
    rec = fake.requests[0]
    assert (rec.method, rec.path, rec.query_names, rec.auth_headers) == (method, path, ("q",), ("authorization",))
    assert "x-custom-thing" in rec.header_names
    dumped = json.dumps(rec.as_dict())
    assert KEY not in dumped and QUERY_VALUE not in dumped and "header-value" not in dumped


def test_fake_control_paths_are_not_recorded(fake):
    with _client(fake) as c:
        assert c.post("/_fake/mode", json={"model": "m", "mode": "ok"}).status_code == 200
        assert c.get("/_fake/requests").status_code == 200
        assert c.get("/_fake/nope").status_code == 404
    assert fake.requests == []


def test_existing_fields_unchanged(fake):
    with _client(fake) as c:
        c.post("/v1/chat/completions", json={"model": "m"}, headers={"Authorization": f"Bearer {KEY}"})
    rec = fake.requests[0]
    assert (rec.method, rec.path, rec.model, rec.has_auth, rec.auth_header) == \
        ("POST", "/v1/chat/completions", "m", True, "authorization")
    assert rec.body == {"model": "m"} and rec.json_body == {"model": "m"}

"""T5 L1:假 API 上游的兩種協定、各模式、延遲、回顯與請求紀錄。"""
from __future__ import annotations

import json
import time
from urllib.parse import urlparse

import httpx
import pytest

from tests.fakes.api_upstream.server import FakeApiServer, ModeSpec

KEY = "sk-test-0000"

OPENAI = ("/v1/chat/completions", {"Authorization": f"Bearer {KEY}"})
ANTHROPIC = ("/v1/messages", {"x-api-key": KEY, "anthropic-version": "2023-06-01"})


def _post(fake: FakeApiServer, proto: tuple[str, dict], model: str, timeout: float = 10.0) -> httpx.Response:
    path, headers = proto
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16}
    return httpx.post(fake.base_url + path, json=body, headers=headers, timeout=timeout)


def _assert_openai_error(resp: httpx.Response) -> dict:
    err = resp.json()["error"]
    assert set(err) == {"message", "type", "param", "code"}
    return err


def _assert_anthropic_error(resp: httpx.Response) -> dict:
    data = resp.json()
    assert data["type"] == "error"
    assert set(data["error"]) == {"type", "message"}
    return data["error"]


def test_binds_loopback_random_port(fake_api):
    url = urlparse(fake_api.base_url)
    assert url.hostname == "127.0.0.1"
    assert url.port and url.port > 0


def test_health_returns_200(fake_api):
    assert httpx.get(fake_api.base_url + "/").status_code == 200


def test_openai_ok_has_content_and_usage(fake_api):
    resp = _post(fake_api, OPENAI, "gpt-4o-mini")
    assert resp.status_code == 200
    data = resp.json()
    assert data["choices"][0]["message"]["content"]
    assert data["usage"]["prompt_tokens"] > 0
    assert data["usage"]["completion_tokens"] > 0


def test_anthropic_ok_has_content_and_usage(fake_api):
    resp = _post(fake_api, ANTHROPIC, "claude-sonnet-4-5")
    assert resp.status_code == 200
    data = resp.json()
    assert data["content"][0]["type"] == "text"
    assert data["content"][0]["text"]
    assert data["usage"]["input_tokens"] > 0
    assert data["usage"]["output_tokens"] > 0


def test_models_list(fake_api):
    fake_api.set_mode("custom-model", "ok")
    resp = httpx.get(fake_api.base_url + "/v1/models")
    assert resp.status_code == 200
    ids = [m["id"] for m in resp.json()["data"]]
    assert "gpt-4o-mini" in ids and "custom-model" in ids


@pytest.mark.parametrize(
    "mode,status,o_type,o_code,a_type",
    [
        ("http_402", 402, "billing_error", "payment_required", "billing_error"),
        ("insufficient_quota", 429, "insufficient_quota", "insufficient_quota", "insufficient_quota"),
        ("http_429", 429, "rate_limit_error", "rate_limit_exceeded", "rate_limit_error"),
        ("http_500", 500, "server_error", None, "api_error"),
    ],
)
def test_error_modes_both_protocols(fake_api, mode, status, o_type, o_code, a_type):
    fake_api.set_mode("m-err", mode)
    resp = _post(fake_api, OPENAI, "m-err")
    assert resp.status_code == status
    err = _assert_openai_error(resp)
    assert (err["type"], err["code"]) == (o_type, o_code)

    resp = _post(fake_api, ANTHROPIC, "m-err")
    assert resp.status_code == status
    assert _assert_anthropic_error(resp)["type"] == a_type


@pytest.mark.parametrize("proto", [OPENAI, ANTHROPIC], ids=["openai", "anthropic"])
def test_delay_mode_waits_at_least_seconds(fake_api, proto):
    fake_api.set_mode("m-slow", "delay", seconds=0.4)
    start = time.monotonic()
    resp = _post(fake_api, proto, "m-slow")
    assert time.monotonic() - start >= 0.4
    assert resp.status_code == 200


def test_echo_auth_openai_echoes_authorization(fake_api):
    fake_api.set_mode("m-echo", "echo_auth")
    resp = _post(fake_api, OPENAI, "m-echo")
    assert resp.status_code == 400
    assert f"Bearer {KEY}" in _assert_openai_error(resp)["message"]


def test_echo_auth_anthropic_echoes_x_api_key(fake_api):
    fake_api.set_mode("m-echo", "echo_auth", status=401)
    resp = _post(fake_api, ANTHROPIC, "m-echo")
    assert resp.status_code == 401
    assert KEY in _assert_anthropic_error(resp)["message"]


def test_modes_are_per_model_and_default_ok(fake_api):
    fake_api.set_mode("m-429", "http_429")
    assert _post(fake_api, OPENAI, "m-429").status_code == 429
    assert _post(fake_api, OPENAI, "unset-model").status_code == 200


def test_wildcard_default_and_reset(fake_api):
    fake_api.set_mode("*", "http_500")
    assert _post(fake_api, OPENAI, "anything").status_code == 500
    fake_api.reset()
    assert _post(fake_api, OPENAI, "anything").status_code == 200
    assert [r.model for r in fake_api.requests] == ["anything"]


def test_requests_are_recorded_without_auth_value(fake_api):
    _post(fake_api, OPENAI, "gpt-4o-mini")
    _post(fake_api, ANTHROPIC, "claude-sonnet-4-5")
    httpx.post(fake_api.base_url + "/v1/chat/completions", json={"model": "no-auth", "messages": []})
    recs = fake_api.requests
    assert [(r.method, r.path, r.model) for r in recs] == [
        ("POST", "/v1/chat/completions", "gpt-4o-mini"),
        ("POST", "/v1/messages", "claude-sonnet-4-5"),
        ("POST", "/v1/chat/completions", "no-auth"),
    ]
    assert [(r.has_auth, r.auth_header) for r in recs] == [
        (True, "authorization"),
        (True, "x-api-key"),
        (False, None),
    ]
    assert recs[0].body["messages"][0]["content"] == "hi"
    serialized = json.dumps([r.as_dict() for r in recs])
    assert KEY not in serialized


def test_invalid_mode_options_rejected():
    with pytest.raises(ValueError):
        ModeSpec.build("nope")
    with pytest.raises(ValueError):
        ModeSpec.build("delay")
    with pytest.raises(ValueError):
        ModeSpec.build("ok", bogus=1)


def test_missing_model_returns_protocol_error(fake_api):
    resp = httpx.post(fake_api.base_url + "/v1/messages", content=b"not json")
    assert resp.status_code == 400
    _assert_anthropic_error(resp)
    resp = httpx.post(fake_api.base_url + "/v1/chat/completions", json={})
    assert resp.status_code == 400
    _assert_openai_error(resp)


def test_control_endpoints(fake_api):
    base = fake_api.base_url
    assert httpx.post(base + "/_fake/mode", json={"model": "m-ctl", "mode": "http_402"}).status_code == 200
    assert _post(fake_api, OPENAI, "m-ctl").status_code == 402
    assert httpx.post(base + "/_fake/mode", json={"model": "m-ctl", "mode": "bad"}).status_code == 400
    recs = httpx.get(base + "/_fake/requests").json()
    assert recs[0]["model"] == "m-ctl" and recs[0]["has_auth"] is True
    assert KEY not in json.dumps(recs)
    assert httpx.post(base + "/_fake/reset").status_code == 200
    assert httpx.get(base + "/_fake/requests").json() == []
    assert _post(fake_api, OPENAI, "m-ctl").status_code == 200


def test_server_stops_cleanly():
    server = FakeApiServer().start()
    url = server.base_url
    assert httpx.get(url + "/").status_code == 200
    server.stop()
    with pytest.raises(httpx.ConnectError):
        httpx.get(url + "/", timeout=1)

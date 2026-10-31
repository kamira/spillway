"""T13 審議回修(CHG-20261030-02 波次 6:M20、M21 與建議修的回顯遮罩)。

- M20 / U8 = (a):`/v1/chat/completions` 只轉交 OpenAI 標準參數白名單,名單外的鍵靜默丟棄;
  `base_url`、`mock_response`、`custom_llm_provider` 都不可改變上游或繞過上游。
- M21:非預期例外一律回 spec 9.1.2 格式的 500 `internal_error`,日誌只記例外型別。
"""
from __future__ import annotations

import logging

import httpx
import pytest

from spillway.api import ALLOWED_CHAT_PARAMS, filter_chat_params, validation_message
from spillway.store import repo
from tests.fakes.api_upstream.server import FakeApiServer
from tests.integration.conftest import BASE_URL, TEST_KEY, TEST_KEY_ENV, api_member

pytestmark = pytest.mark.anyio

MSGS = [{"role": "user", "content": "hello"}]


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY)


@pytest.fixture
def second_upstream():
    server = FakeApiServer().start()
    try:
        yield server
    finally:
        server.stop()


def _chat_route(h, base_url):
    h.add_deployment(api_member("oa-1", base_url))
    h.add_route(repo.Route(name="chat", target={"tags": {"all": ["chat"]}}))


def _chat_posts(fake):
    return [x for x in fake.requests if x.path.endswith("/chat/completions")]


# ---------------------------------------------------------------- M20 白名單


def test_whitelist_is_exactly_the_user_decision():
    assert ALLOWED_CHAT_PARAMS == {
        "temperature", "top_p", "n", "stop", "presence_penalty", "frequency_penalty",
        "logit_bias", "user", "response_format", "seed", "tool_choice", "parallel_tool_calls",
        "logprobs", "top_logprobs", "max_completion_tokens",
    }
    assert filter_chat_params({"temperature": 0.2, "base_url": "x", "api_base": "y"}) == {
        "temperature": 0.2}
    assert filter_chat_params(None) == {}


@pytest.mark.parametrize("key", ["base_url", "api_base"])
async def test_body_cannot_redirect_to_another_upstream(spillway, fake_api, second_upstream, key):
    _chat_route(spillway, fake_api.base_url)
    r = await spillway.client.post("/v1/chat/completions", json={
        "model": "chat", "messages": MSGS, key: f"{second_upstream.base_url}/v1"})
    assert r.status_code == 200, r.text
    assert second_upstream.requests == []               # 金鑰不會被送到第二個上游
    posts = _chat_posts(fake_api)
    assert len(posts) == 1 and posts[0].has_auth
    assert key not in (posts[0].body or {})


async def test_mock_response_does_not_bypass_upstream(spillway, fake_api):
    _chat_route(spillway, fake_api.base_url)
    r = await spillway.client.post("/v1/chat/completions", json={
        "model": "chat", "messages": MSGS, "mock_response": "MOCKED"})
    assert r.status_code == 200, r.text
    assert "MOCKED" not in r.text
    assert r.json()["choices"][0]["message"]["content"] == "fake upstream ok"
    assert len(_chat_posts(fake_api)) == 1               # 照常打上游


async def test_custom_llm_provider_does_not_change_protocol(spillway, fake_api):
    _chat_route(spillway, fake_api.base_url)
    r = await spillway.client.post("/v1/chat/completions", json={
        "model": "chat", "messages": MSGS, "custom_llm_provider": "anthropic"})
    assert r.status_code == 200, r.text
    paths = [x.path for x in fake_api.requests]
    assert paths == ["/v1/chat/completions"]             # 不是 /v1/messages


async def test_whitelisted_param_reaches_upstream(spillway, fake_api):
    _chat_route(spillway, fake_api.base_url)
    r = await spillway.client.post("/v1/chat/completions", json={
        "model": "chat", "messages": MSGS, "temperature": 0.3, "seed": 7,
        "extra_headers": {"X-Leak": "1"}, "unknown_vendor_flag": True})
    assert r.status_code == 200, r.text
    body = _chat_posts(fake_api)[0].body
    assert body["temperature"] == 0.3
    assert body["seed"] == 7
    assert "unknown_vendor_flag" not in body


def test_admin_chat_body_uses_the_same_filter():
    from spillway.api.admin import ChatBody

    body = ChatBody.model_validate({"messages": MSGS, "temperature": 0.1, "base_url": "http://x",
                                    "mock_response": "MOCKED"})
    assert body.to_request("chat").extra == {"temperature": 0.1}


# ---------------------------------------------------------------- M21 全站 500


async def test_models_db_failure_is_9_1_2_internal_error(spillway, caplog):
    def broken():
        raise RuntimeError("db exploded sk-test-0000")

    spillway.app.state.conn_factory = broken
    transport = httpx.ASGITransport(app=spillway.app, raise_app_exceptions=False)
    with caplog.at_level(logging.ERROR):
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            r = await client.get("/v1/models")
    assert r.status_code == 500
    assert r.json() == {"error": {"code": "internal_error", "message": "內部錯誤(RuntimeError)"}}
    assert "RuntimeError" in caplog.text
    assert "db exploded" not in caplog.text and "sk-test-0000" not in caplog.text


def test_validation_message_empty_loc_outputs_msg_only():
    assert validation_message([{"loc": (), "msg": "Input should be a valid dictionary"}]) == \
        "Input should be a valid dictionary"
    assert validation_message([{"loc": ("body", "x"), "msg": "bad"}]) == "body.x: bad"
    assert validation_message([]) == "請求格式錯誤"


def test_validation_message_redacts_key_names():
    msg = validation_message([{"loc": ("body", "sk-test-0000abcd"), "msg": "Extra inputs"}])
    assert "sk-test-0000" not in msg


# ---------------------------------------------------------------- 建議修:回顯遮罩


async def test_route_not_found_message_redacts_model(spillway):
    r = await spillway.client.post("/v1/chat/completions", json={
        "model": "sk-test-0000abcdef", "messages": MSGS})
    assert r.status_code == 404
    assert "sk-test-0000" not in r.text

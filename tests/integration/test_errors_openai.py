"""api.openai 的錯誤回應(CHG-20261030-02 T13;spec 4.1.4、4.5.3、9.1.2;決策 7、16、29、31、35)。

每個錯誤 body 都必須正好是 `{"error": {"code": str, "message": str}}`。
"""
from __future__ import annotations

import sqlite3

import pytest

from spillway.store import repo
from tests.integration.conftest import TEST_KEY, TEST_KEY_ENV, api_member

pytestmark = pytest.mark.anyio

MSGS = [{"role": "user", "content": "hello"}]


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY)


def assert_error(r, status: int, code: str) -> str:
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    assert r.headers["content-type"].startswith("application/json")
    return body["error"]["message"]


def _two_slot_route(h, base_url):
    h.add_deployment(api_member("a", base_url, model="gpt-4o"))
    h.add_deployment(api_member("b", base_url, model="gpt-4o-mini"))
    h.add_route(repo.Route(name="chat", target={"tags": {"all": ["chat"]}}),
                [repo.RouteTarget("a", 1, 0), repo.RouteTarget("b", 2, 0)])


async def test_stream_true_is_400_and_no_upstream_call(spillway, fake_api):
    _two_slot_route(spillway, fake_api.base_url)
    r = await spillway.client.post("/v1/chat/completions",
                                   json={"model": "chat", "messages": MSGS, "stream": True})
    assert_error(r, 400, "streaming_unsupported")
    assert fake_api.requests == []
    assert spillway.ledger() == []


async def test_unknown_model_is_404_route_not_found(spillway):
    r = await spillway.client.post("/v1/chat/completions", json={"model": "nope", "messages": MSGS})
    msg = assert_error(r, 404, "route_not_found")
    assert "nope" in msg


@pytest.mark.parametrize("effort", ["turbo", "", 3])
async def test_invalid_reasoning_effort_is_422(spillway, fake_api, effort):
    _two_slot_route(spillway, fake_api.base_url)
    r = await spillway.client.post("/v1/chat/completions", json={
        "model": "chat", "messages": MSGS, "reasoning_effort": effort,
    })
    if effort == "":
        assert r.status_code == 200   # 空白字串視為沒帶(決策 29 的 normalize_effort)
        return
    msg = assert_error(r, 422, "validation_error")
    assert "reasoning_effort" in msg
    assert fake_api.requests == []


@pytest.mark.parametrize(
    ("payload", "path"),
    [
        ({"messages": MSGS}, "body.model"),
        ({"model": "chat"}, "body.messages"),
        ({"model": "chat", "messages": "hi"}, "body.messages"),
        ({"model": "chat", "messages": [{"content": "x"}]}, "body.messages.0.role"),
        ({"model": "chat", "messages": MSGS, "stream": "yes"}, "body.stream"),
        ({"model": "chat", "messages": MSGS, "max_tokens": 0}, "body.max_tokens"),
    ],
)
async def test_body_validation_is_422_with_field_path(spillway, payload, path):
    r = await spillway.client.post("/v1/chat/completions", json=payload)
    msg = assert_error(r, 422, "validation_error")
    assert path in msg


async def test_validation_message_does_not_echo_input(spillway):
    r = await spillway.client.post("/v1/chat/completions",
                                   json={"model": "chat", "messages": MSGS, "max_tokens": TEST_KEY})
    assert_error(r, 422, "validation_error")
    assert TEST_KEY not in r.text


async def test_malformed_json_is_422(spillway):
    r = await spillway.client.post("/v1/chat/completions", content=b"{not json",
                                   headers={"content-type": "application/json"})
    assert_error(r, 422, "validation_error")


async def test_unknown_path_and_method_use_error_format(spillway):
    assert_error(await spillway.client.get("/v1/nope"), 404, "not_found")
    assert_error(await spillway.client.get("/v1/chat/completions"), 405, "method_not_allowed")


async def test_all_failed_is_503_listing_attempts(spillway, fake_api):
    _two_slot_route(spillway, fake_api.base_url)
    fake_api.set_mode("gpt-4o", "http_429")
    fake_api.set_mode("gpt-4o-mini", "http_500")
    r = await spillway.client.post("/v1/chat/completions", json={"model": "chat", "messages": MSGS})
    msg = assert_error(r, 503, "no_candidate")
    # 與 RouteOutcome.attempts(= 帳本,依發生順序)一致
    rows = list(reversed(spillway.ledger()))
    assert [(x.deployment_id, x.status) for x in rows] == [("a", "rate_limited"), ("b", "error")]
    assert "a:rate_limited" in msg and "b:error" in msg
    assert msg.index("a:rate_limited") < msg.index("b:error")
    assert "被篩" not in msg

    # 第二次:a 冷卻中被篩掉,只嘗試 b(決策 31:先列嘗試,再列被篩成員與原因)
    r2 = await spillway.client.post("/v1/chat/completions", json={"model": "chat", "messages": MSGS})
    msg2 = assert_error(r2, 503, "no_candidate")
    assert "b:error" in msg2 and "a:cooling" in msg2
    assert msg2.index("b:error") < msg2.index("a:cooling")
    assert "a:rate_limited" not in msg2


async def test_503_attempt_list_matches_route_outcome(spillway, fake_api, monkeypatch):
    """直接比對 Router 回傳的 RouteOutcome.attempts 與 503 message。"""
    from spillway.routing import Router

    _two_slot_route(spillway, fake_api.base_url)
    fake_api.set_mode("*", "http_500")
    seen = []
    real = Router.route

    async def spy(self, req):
        out = await real(self, req)
        seen.append(out)
        return out

    monkeypatch.setattr(Router, "route", spy)
    r = await spillway.client.post("/v1/chat/completions", json={"model": "chat", "messages": MSGS})
    msg = assert_error(r, 503, "no_candidate")
    (outcome,) = seen
    assert outcome.result is None and len(outcome.attempts) == 2
    pos = [msg.index(f"{a.deployment}:{a.status}") for a in outcome.attempts]
    assert pos == sorted(pos)


async def test_503_does_not_echo_upstream_key(spillway, fake_api):
    """假上游 echo_auth 會把 `Bearer sk-test-0000` 放進錯誤訊息;503 body 不可含金鑰。"""
    _two_slot_route(spillway, fake_api.base_url)
    fake_api.set_mode("*", "echo_auth")
    r = await spillway.client.post("/v1/chat/completions", json={"model": "chat", "messages": MSGS})
    assert_error(r, 503, "no_candidate")
    assert TEST_KEY not in r.text
    assert "0000" not in r.text


async def test_local_direct_disabled_is_503(spillway, fake_api):
    from tests.integration.conftest import local_member

    spillway.add_deployment(local_member("loc-1", fake_api.base_url, enabled=False))
    r = await spillway.client.post("/v1/chat/completions",
                                   json={"model": "local/loc-1", "messages": MSGS})
    msg = assert_error(r, 503, "no_candidate")
    assert "loc-1:disabled" in msg


async def test_ledger_write_failure_is_500(spillway, fake_api, monkeypatch, caplog):
    """決策 35:帳本寫入失敗時整個請求失敗(500),訊息與日誌不含秘密。"""
    from spillway.ledger import LedgerWriter

    def boom(self, attempt):
        raise sqlite3.OperationalError(f"disk I/O error {TEST_KEY}")

    monkeypatch.setattr(LedgerWriter, "record", boom)
    _two_slot_route(spillway, fake_api.base_url)
    r = await spillway.client.post("/v1/chat/completions", json={"model": "chat", "messages": MSGS})
    assert_error(r, 500, "internal_error")
    assert TEST_KEY not in r.text
    assert TEST_KEY not in caplog.text
    assert "hello" not in caplog.text   # 不記錄請求內容

"""`POST /v1/chat/completions` 成功路徑與 `GET /v1/models`(CHG-20261030-02 T13;spec 9.1.3、9.2、5.4.5;決策 18、28)。"""
from __future__ import annotations

import pytest

from spillway.store import repo
from tests.contract.fakemods import write_module
from tests.integration.conftest import (
    TEST_KEY,
    TEST_KEY_ENV,
    api_member,
    local_member,
)

pytestmark = pytest.mark.anyio

MSGS = [{"role": "user", "content": "hello"}]


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY)


def _chat_route(h, base_url, *, name="chat"):
    h.add_deployment(api_member("oa-1", base_url))
    h.add_route(repo.Route(name=name, target={"tags": {"all": ["chat"]}}))


async def test_success_body_and_headers_match_ledger(spillway, fake_api):
    _chat_route(spillway, fake_api.base_url)
    r = await spillway.client.post("/v1/chat/completions", json={"model": "chat", "messages": MSGS})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["model"] == "chat"                       # 請求的 model,不是成員
    assert body["created"] == int(spillway.clock.now().timestamp())
    choice = body["choices"][0]
    assert choice["index"] == 0
    assert choice["message"] == {"role": "assistant", "content": "fake upstream ok"}
    assert choice["finish_reason"] == "stop"
    assert body["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}

    rows = spillway.ledger()
    assert len(rows) == 1 and rows[0].status == "ok"
    assert r.headers["X-Spillway-Served-By"] == rows[0].deployment_id == "oa-1"
    assert r.headers["X-Spillway-Slot"] == str(rows[0].slot) == "2"   # 無 cli 時 api 為 slot 2(決策 38)
    assert rows[0].est_cost_usd_p90 is not None
    assert float(r.headers["X-Spillway-Est-Cost"]) == pytest.approx(rows[0].est_cost_usd_p90, abs=1e-8)


async def test_served_by_is_the_fallback_member(spillway, fake_api):
    spillway.add_deployment(api_member("a", fake_api.base_url, model="gpt-4o"))
    spillway.add_deployment(api_member("b", fake_api.base_url, model="gpt-4o-mini"))
    spillway.add_route(repo.Route(name="chat", target={"tags": {"all": ["chat"]}}),
                       [repo.RouteTarget("a", 1, 0), repo.RouteTarget("b", 2, 0)])
    fake_api.set_mode("gpt-4o", "http_500")
    r = await spillway.client.post("/v1/chat/completions", json={"model": "chat", "messages": MSGS})
    assert r.status_code == 200, r.text
    ok_row = next(x for x in spillway.ledger() if x.status == "ok")
    assert r.headers["X-Spillway-Served-By"] == ok_row.deployment_id == "b"
    assert r.headers["X-Spillway-Slot"] == "2"


async def test_implicit_route_by_model_name(spillway, fake_api):
    spillway.add_deployment(api_member("oa-1", fake_api.base_url, model_name="shared"))
    r = await spillway.client.post("/v1/chat/completions", json={"model": "shared", "messages": MSGS})
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "shared"
    assert spillway.ledger()[0].route_name == "shared"


async def test_local_direct_slot_header_is_direct(spillway, fake_api):
    spillway.add_deployment(local_member("loc-1", fake_api.base_url))
    r = await spillway.client.post("/v1/chat/completions",
                                   json={"model": "local/loc-1", "messages": MSGS})
    assert r.status_code == 200, r.text
    assert r.headers["X-Spillway-Slot"] == "direct"
    assert r.headers["X-Spillway-Served-By"] == "loc-1"
    row = spillway.ledger()[0]
    assert row.slot is None and row.route_name is None
    # hosted_vllm 無定價 → 預估花費 None → header 存在但為空字串
    assert row.est_cost_usd_p90 is None
    assert "X-Spillway-Est-Cost" in r.headers and r.headers["X-Spillway-Est-Cost"] == ""


async def test_unknown_fields_go_to_upstream_extra(spillway, fake_api):
    _chat_route(spillway, fake_api.base_url)
    r = await spillway.client.post("/v1/chat/completions", json={
        "model": "chat", "messages": MSGS, "temperature": 0.25, "max_tokens": 64,
    })
    assert r.status_code == 200, r.text
    sent = [q for q in fake_api.requests if q.path == "/v1/chat/completions"][-1].body
    assert sent["temperature"] == 0.25
    assert sent["max_tokens"] == 64


async def test_reasoning_effort_minimal_is_normalized(spillway, fake_api):
    _chat_route(spillway, fake_api.base_url)
    r = await spillway.client.post("/v1/chat/completions", json={
        "model": "chat", "messages": MSGS, "reasoning_effort": "MINIMAL",
    })
    assert r.status_code == 200, r.text


TOOL_ADAPTER = '''\
from spillway.adapters.base import CallResult


class FakeAdapter:
    def __init__(self, context):
        self.context = context

    async def call(self, dep, req, effort):
        return CallResult(ok=True, text="", tokens_in=3, tokens_out=4, cost_usd=0.0, status="ok",
                          error=None, finish_reason="tool_calls",
                          tool_calls=[{"id": "call_1", "type": "function",
                                       "function": {"name": "lookup", "arguments": "{\\"q\\": 1}"}}])

    async def health(self, dep):
        return True
'''


async def test_tool_calls_are_returned(open_spillway, tmp_path):
    from spillway.domain import Deployment

    write_module(tmp_path / "mods", "toolmod", providers=("toolprov",), adapter_source=TOOL_ADAPTER)
    async with open_spillway(extra_adapter_paths=[tmp_path / "mods"]) as h:
        h.add_deployment(Deployment(id="t1", model_name="tooly", kind="api", provider="toolprov",
                                    model="x", credential_ref=None,
                                    capabilities={"ctx": 8000, "tools": True}))
        r = await h.client.post("/v1/chat/completions", json={
            "model": "tooly", "messages": MSGS,
            "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {}}}],
        })
        assert r.status_code == 200, r.text
        choice = r.json()["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        assert choice["message"]["content"] is None
        assert choice["message"]["tool_calls"] == [
            {"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": '{"q": 1}'}}
        ]
        assert r.json()["usage"] == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}


async def test_models_list_matches_database(spillway, fake_api):
    spillway.add_deployment(api_member("oa-1", fake_api.base_url, model_name="gpt-4o-mini"))
    spillway.add_deployment(api_member("oa-2", fake_api.base_url, model_name="gpt-4o-mini"))
    spillway.add_deployment(local_member("loc-1", fake_api.base_url, model_name="llama"))
    spillway.add_route(repo.Route(name="chat", target={"tags": {"all": ["chat"]}}))
    spillway.add_route(repo.Route(name="llama", target={"tags": {"all": ["chat"]}}))  # 與 model_name 同名
    r = await spillway.client.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    assert ids == ["chat", "llama", "gpt-4o-mini", "local/loc-1"]
    assert len(ids) == len(set(ids))
    assert all(m["object"] == "model" and m["owned_by"] == "spillway" for m in body["data"])

    with spillway.db() as conn:
        expected = {rt.name for rt in repo.list_routes(conn)}
        expected |= {d.model_name for d in repo.list_deployments(conn)}
        expected |= {f"local/{d.id}" for d in repo.list_deployments(conn) if d.kind == "local"}
    assert set(ids) == expected


async def test_models_list_empty_database(spillway):
    r = await spillway.client.get("/v1/models")
    assert r.status_code == 200
    assert r.json() == {"object": "list", "data": []}

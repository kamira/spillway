"""tests/integration/admin 的測試資料輔助(CHG-20261030-02 T14)。"""
from __future__ import annotations

from typing import Any

import httpx

KEY = "sk-test-0000"



def api_member(dep_id: str, *, provider: str = "openai", model: str = "gpt-4o-mini",
               tags: list[str] | None = None, fake_api: Any = None, **kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": dep_id,
        "model_name": dep_id,
        "kind": "api",
        "provider": provider,
        "model": model,
        "credential_ref": "env:SPILLWAY_TEST_KEY",
        "capabilities": {"ctx": 100000, "tools": True, "vision": True,
                         "efforts": ["low", "medium", "high"]},
        "tags": tags if tags is not None else ["r"],
    }
    if fake_api is not None:
        body["params"] = {"api_base": fake_api.base_url + "/v1"}
    body.update(kw)
    return body


def local_member(dep_id: str, *, tags: list[str] | None = None, fake_api: Any = None,
                 **kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": dep_id,
        "model_name": dep_id,
        "kind": "local",
        "provider": "hosted_vllm",
        "model": "local-model",
        "capabilities": {"ctx": 32768, "tools": False, "vision": False, "efforts": []},
        "tags": tags if tags is not None else ["r"],
    }
    if fake_api is not None:
        body["params"] = {"api_base": fake_api.base_url + "/v1"}
    body.update(kw)
    return body


def assert_error(resp: httpx.Response, status: int, code: str) -> str:
    """9.1.2 格式;回傳 message。"""
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"error"}, body
    assert set(body["error"]) == {"code", "message"}, body
    assert body["error"]["code"] == code, body
    assert isinstance(body["error"]["message"], str)
    return body["error"]["message"]

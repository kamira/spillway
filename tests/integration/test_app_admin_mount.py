"""`create_app()` 掛載 admin.router 後(CHG-20261030-02 T15):同一個 app 的 `/api/*` 與 `/v1/*`。

- 管理端點經 `create_app()` 可用(不再只有 T14 測試自組的 app)。
- 422 一律 spec 9.1.2 格式(`{"error": {"code": "validation_error", "message"}}`),兩邊 message 格式一致:
  「欄位路徑: 原因」,多筆以「; 」連接,路徑以 `.` 連接(決策 16);不回顯輸入值。
- 未知路徑、方法不允許在 `/api/*` 與 `/v1/*` 也是同一格式(決策 42)。
"""
from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.anyio

# 「body.<路徑>: <原因>」;多筆以「; 」分隔
_PART = r"body(\.[^\s:;]+)*: [^;]+"
MESSAGE_RE = re.compile(rf"^{_PART}(; {_PART})*$")


def _error(resp, status: int, code: str) -> str:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message"}, body
    assert body["error"]["code"] == code, body
    return body["error"]["message"]


async def test_admin_router_is_mounted(spillway):
    r = await spillway.client.get("/api/dashboard")
    assert r.status_code == 200, r.text
    assert r.json()["deployments"] == []
    r = await spillway.client.get("/api/adapters")
    assert r.status_code == 200
    assert {"anthropic_api", "openai_api", "litellm"} <= {m["module"] for m in r.json()["modules"]}


async def test_422_same_format_on_api_and_v1(spillway):
    c = spillway.client
    # 同一類錯誤(缺必要欄位)在兩邊:路徑 + Pydantic 原因
    admin_missing = _error(await c.post("/api/deployments", json={"id": "x"}), 422, "validation_error")
    v1_missing = _error(await c.post("/v1/chat/completions", json={"model": "m"}), 422, "validation_error")
    assert MESSAGE_RE.match(admin_missing), admin_missing
    assert MESSAGE_RE.match(v1_missing), v1_missing
    assert "body.model_name: Field required" in admin_missing.split("; ")
    assert v1_missing == "body.messages: Field required"

    # 巢狀路徑以 . 連接(含清單索引)
    admin_nested = _error(await c.post("/api/routes/x/dry-run", json={"messages": "not-a-list"}),
                          422, "validation_error")
    v1_nested = _error(await c.post("/v1/chat/completions",
                                    json={"model": "m", "messages": [{"content": "hi"}]}),
                       422, "validation_error")
    assert MESSAGE_RE.match(admin_nested) and admin_nested.startswith("body.messages: "), admin_nested
    assert v1_nested == "body.messages.0.role: Field required"

    # 不回顯輸入值(可能是誤貼的金鑰)
    secret = "sk-test-0000"
    r1 = await c.post("/api/deployments", json={"id": "x", "model_name": 1, "kind": secret})
    r2 = await c.post("/v1/chat/completions", json={"model": "m", "messages": [], "max_tokens": secret})
    for r in (r1, r2):
        msg = _error(r, 422, "validation_error")
        assert MESSAGE_RE.match(msg), msg
        assert secret not in r.text


async def test_404_405_same_format_on_api_and_v1(spillway):
    c = spillway.client
    for path in ("/api/no-such", "/v1/no-such"):
        _error(await c.get(path), 404, "not_found")
    _error(await c.delete("/api/dashboard"), 405, "method_not_allowed")
    _error(await c.delete("/v1/chat/completions"), 405, "method_not_allowed")

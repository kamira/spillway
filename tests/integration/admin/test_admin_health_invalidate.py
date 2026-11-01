"""管理 API 修改/刪除成員後的健康快取失效與 DEBUG 日誌不含 prompt(CHG-20261101-02 T8 L2;決策 12、13)。

健康快取只在 route 路徑的 `CandidateFilter` 讀寫(`local/<id>` 直呼不看),所以一律經「target 只含
local 成員 `loc-1` 的 route」送請求。FakeClock 全程不前進:第二次請求若仍讀到舊的快取結果
(10 秒 TTL 內)就會回 503,只有失效才會以新設定重新探測。
"""
from __future__ import annotations

import logging
import socket
from typing import Any

import pytest

from spillway.routing import Router

from .helpers import assert_error, local_member

pytestmark = pytest.mark.anyio

ROUTE = "lr"
MSGS = [{"role": "user", "content": "hello"}]


def _dead_base() -> str:
    """一個當下沒有服務在聽的 127.0.0.1 埠(綁定後立即關閉)。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


async def _setup(h: Any, api_base: str) -> dict[str, Any]:
    body = local_member("loc-1", tags=["loc-1"], params={"api_base": api_base})
    r = await h.client.post("/api/deployments", json=body)
    assert r.status_code == 201, r.text
    r = await h.client.post("/api/routes", json={
        "name": ROUTE, "target": {"tags": {"all": [], "any": ["loc-1"], "none": []}}})
    assert r.status_code == 201, r.text
    r = await h.client.put(f"/api/routes/{ROUTE}/order",
                           json={"slots": [[{"deployment": "loc-1", "weight": "medium"}]]})
    assert r.status_code == 200, r.text
    return body


async def _chat(h: Any, messages: list[dict[str, Any]] = MSGS):
    return await h.client.post("/v1/chat/completions", json={"model": ROUTE, "messages": messages})


async def _first_unhealthy_then_put(h: Any, fake_api: Any) -> Any:
    body = await _setup(h, _dead_base())
    cache = h.app.state.router.health_cache
    t0 = h.clock.now()
    r1 = await _chat(h)
    assert_error(r1, 503, "no_candidate")
    assert cache.get("loc-1") is False          # 探測失敗已記為不健康
    put = await h.client.put("/api/deployments/loc-1",
                             json={**body, "params": {"api_base": fake_api.base_url + "/v1"}})
    assert put.status_code == 200, put.text
    assert h.clock.now() == t0                   # 系統時間不前進
    return await _chat(h)


async def test_put_invalidates_health_next_routed_request_uses_new_api_base(open_spillway, fake_api):
    async with open_spillway() as h:
        r2 = await _first_unhealthy_then_put(h, fake_api)
        assert r2.status_code == 200, r2.text
        assert r2.headers["X-Spillway-Served-By"] == "loc-1"
        assert h.app.state.router.health_cache.get("loc-1") is True


async def test_control_without_invalidate_second_request_still_503(open_spillway, fake_api, monkeypatch):
    """對照組:`invalidate_health` 不做事時,同樣步驟的第二次請求沿用快取的不健康結果。"""
    monkeypatch.setattr(Router, "invalidate_health", lambda self, dep_id: None)
    async with open_spillway() as h:
        r2 = await _first_unhealthy_then_put(h, fake_api)
        assert_error(r2, 503, "no_candidate")


async def test_put_enabled_change_also_invalidates(open_spillway, fake_api):
    """`enabled` 只能經 PUT 變更(沒有獨立的啟停端點);PUT 成功即失效。"""
    async with open_spillway() as h:
        body = await _setup(h, fake_api.base_url + "/v1")
        cache = h.app.state.router.health_cache
        assert (await _chat(h)).status_code == 200
        assert cache.get("loc-1") is True
        put = await h.client.put("/api/deployments/loc-1", json={**body, "enabled": False})
        assert put.status_code == 200, put.text
        assert cache.get("loc-1") is None


async def test_failed_put_does_not_invalidate(open_spillway, fake_api):
    async with open_spillway() as h:
        body = await _setup(h, fake_api.base_url + "/v1")
        cache = h.app.state.router.health_cache
        assert (await _chat(h)).status_code == 200
        bad = await h.client.put("/api/deployments/loc-1", json={**body, "id": "other"})
        assert bad.status_code == 422, bad.text
        assert cache.get("loc-1") is True


async def test_delete_removes_id_from_health_cache(open_spillway, fake_api):
    async with open_spillway() as h:
        await _setup(h, fake_api.base_url + "/v1")
        cache = h.app.state.router.health_cache
        assert (await _chat(h)).status_code == 200
        assert cache.get("loc-1") is True
        d = await h.client.delete("/api/deployments/loc-1")
        assert d.status_code == 204, d.text
        assert cache.get("loc-1") is None


async def test_debug_root_logger_does_not_log_prompt(open_spillway, fake_api, caplog, capfd):
    """root logger 為 DEBUG 時,經 route 的請求不會讓 LiteLLM/SDK/httpx 把 prompt 印進日誌。

    caplog 只看得到會 propagate 到 root 的紀錄;LiteLLM 另掛自己的 stderr handler,所以也檢查
    `capfd` 擷取的 stderr(決策審議 D19)。
    """
    prompt = "zebra-quokka-prompt-7731"
    root = logging.getLogger()
    saved = root.level
    caplog.set_level(logging.DEBUG)
    try:
        async with open_spillway() as h:
            await _setup(h, fake_api.base_url + "/v1")
            capfd.readouterr()
            caplog.clear()
            r = await _chat(h, [{"role": "user", "content": prompt}])
            assert r.status_code == 200, r.text
            assert r.headers["X-Spillway-Served-By"] == "loc-1"
            assert root.isEnabledFor(logging.DEBUG)
            err = capfd.readouterr().err
            assert prompt not in caplog.text
            assert prompt not in err
    finally:
        root.setLevel(saved)

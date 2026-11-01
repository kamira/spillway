"""m2c_cleanup.feature 第 3 情境(健康快取失效)的專屬步驟(CHG-20261101-02 T8;決策 12)。

只新增兩句:「local 成員 …,api_base 指向一個沒有服務的埠」與「客戶端以 model … 送出 1 次請求,
系統時間不前進」。route 順序、回應狀態與 header 沿用 `common_steps.py`;「以管理 API 把成員 … 的
api_base 改為指向假 API 上游」由 T7 的 `cleanup_admin_steps.py` 提供。
"""
from __future__ import annotations

import socket

from behave import step, use_step_matcher

use_step_matcher("re")


def _dead_base() -> str:
    """一個當下沒有服務在聽的 127.0.0.1 埠(綁定後立即關閉)。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


@step(r'local 成員 "([^"]+)",provider "([^"]+)",api_base 指向一個沒有服務的埠')
def step_local_member_dead_port(context, dep_id, provider):
    gw = context.gw
    body = gw.local_body(dep_id, provider)
    body["params"] = {**body.get("params", {}), "api_base": _dead_base()}
    gw.ok(gw.create_member(body), 201)
    dep = gw.deployment(dep_id)
    assert not dep["params"]["api_base"].startswith(gw.fake.base_url), dep
    # 之後「系統時間不前進」以此時刻為基準
    context.health_t0 = gw.clock.now()


@step(r'客戶端以 model "([^"]+)" 送出 1 次請求,系統時間不前進')
def step_send_once_clock_frozen(context, model):
    gw = context.gw
    t0 = getattr(context, "health_t0", None)
    assert t0 is not None, "缺少「api_base 指向一個沒有服務的埠」前置步驟"
    assert gw.clock.now() == t0, f"系統時間已前進:{t0} → {gw.clock.now()}"
    gw.chat(model)
    assert gw.clock.now() == t0
    # 健康快取仍在 TTL 內:第二次請求若成功,只可能是管理 API 修改後已失效重新探測
    cached = gw.app.state.router.health_cache
    if gw.last.status_code == 200:
        assert cached.get(gw.last.headers.get("X-Spillway-Served-By", "")) is True

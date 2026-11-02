"""`notifications/tools/list_changed`(CHG-20261102-01 T3 L1;決策 13;AM-9)。

以 `live_gateway`(真實 uvicorn;GET SSE 通知串流不被緩衝)+ 官方 SDK 用戶端:session 先呼叫一次
`tools/list`(登記)、以 `wait_stream_open` 輪詢到 GET 串流在伺服器端就緒,之後經管理 API 做 route 建立、
更新(目標改到別的群組)、刪除、order 儲存/還原、群組建立/更新/刪除,每次每個登記的 session 各收到一次
通知;重取 `tools/list` 反映變更。`McpEvents.changed()` 對關閉中或卡住的 session 不拖住管理 API。
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import anyio
import pytest
from mcp import types

from spillway.api.mcp import events as events_mod
from spillway.api.mcp.events import McpEvents

pytestmark = pytest.mark.anyio

WAIT = 5.0       # 等通知的上限(真實時間)
QUIET = 0.3      # 確認「沒有多送」的觀察時間


class Inbox:
    """收集 `ToolListChangedNotification` 的 message_handler。"""

    def __init__(self) -> None:
        self.count = 0
        self._cond: asyncio.Condition | None = None

    async def handler(self, message: Any) -> None:
        if isinstance(message, types.ServerNotification) and isinstance(
                message.root, types.ToolListChangedNotification):
            self.count += 1

    async def wait_count(self, n: int) -> None:
        async def _poll() -> None:
            while self.count < n:
                await asyncio.sleep(0.02)

        await asyncio.wait_for(_poll(), WAIT)
        await asyncio.sleep(QUIET)
        assert self.count == n, f"預期 {n} 次通知,實際 {self.count}"


def _enum(tools: list[types.Tool]) -> list[str] | None:
    ask = next(t for t in tools if t.name == "ask")
    return ask.inputSchema["properties"]["route"].get("enum")


async def _admin(gw: Any, method: str, path: str, json: Any = None, expect: int = 200) -> float:
    start = time.monotonic()
    async with gw.async_http() as http:
        r = await http.request(method, path, json=json)
    assert r.status_code == expect, r.text
    return time.monotonic() - start


async def test_admin_changes_notify_every_registered_session(seeded_gateway):
    gw = seeded_gateway
    a, b, idle = Inbox(), Inbox(), Inbox()
    async with gw.mcp_session("coding", message_handler=a.handler) as ca, \
            gw.mcp_session("ops", message_handler=b.handler) as cb, \
            gw.mcp_session("all", message_handler=idle.handler) as cidle:
        assert _enum((await ca.session.list_tools()).tools) == ["review", "review-b"]
        assert _enum((await cb.session.list_tools()).tools) == ["deploy"]
        # cidle 沒有列過工具 → 不登記(決策 13)
        for c in (ca, cb, cidle):
            await gw.wait_stream_open(c.session_id)

        steps: list[tuple[str, str, Any, int, Callable[[list[str] | None, list[str] | None], None] | None]] = [
            ("POST", "/api/routes", {"name": "new-r", "target": {"group": "coding"}}, 201,
             lambda ea, eb: (ea == ["new-r", "review", "review-b"] and eb == ["deploy"]) or _fail(ea, eb)),
            ("PUT", "/api/routes/new-r", {"target": {"group": "ops"}}, 200,
             lambda ea, eb: (ea == ["review", "review-b"] and eb == ["deploy", "new-r"]) or _fail(ea, eb)),
            ("PUT", "/api/routes/review/order", {"slots": [[{"deployment": "api-a"}]]}, 200, None),
            ("DELETE", "/api/routes/review/order", None, 204, None),
            ("DELETE", "/api/routes/new-r", None, 204,
             lambda ea, eb: (ea == ["review", "review-b"] and eb == ["deploy"]) or _fail(ea, eb)),
            ("POST", "/api/groups", {"name": "g2", "selector": {"all": ["a"]}}, 201, None),
            ("PUT", "/api/groups/g2", {"selector": {"all": ["b"]}}, 200, None),
            ("DELETE", "/api/groups/g2", None, 204, None),
        ]
        for n, (method, path, body, status, check) in enumerate(steps, start=1):
            await _admin(gw, method, path, body, status)
            await a.wait_count(n)
            await b.wait_count(n)
            if check is not None:   # 重取 tools/list 反映變更
                check(_enum((await ca.session.list_tools()).tools),
                      _enum((await cb.session.list_tools()).tools))
        assert idle.count == 0


def _fail(ea: Any, eb: Any) -> None:
    raise AssertionError(f"tools/list 未反映變更:coding={ea} ops={eb}")


async def test_call_tool_registers_session(seeded_gateway):
    gw = seeded_gateway
    inbox = Inbox()
    async with gw.mcp_session("coding", message_handler=inbox.handler) as c:
        await c.session.call_tool("list_routes", {})
        await gw.wait_stream_open(c.session_id)
        await _admin(gw, "POST", "/api/routes", {"name": "r2", "target": {"group": "coding"}}, 201)
        await inbox.wait_count(1)


async def test_failed_admin_write_does_not_notify(seeded_gateway):
    gw = seeded_gateway
    inbox = Inbox()
    async with gw.mcp_session("coding", message_handler=inbox.handler) as c:
        await c.session.list_tools()
        await gw.wait_stream_open(c.session_id)
        await _admin(gw, "POST", "/api/routes", {"name": "review", "target": {"group": "coding"}}, 409)
        await _admin(gw, "DELETE", "/api/groups/coding", None, 409)
        await asyncio.sleep(QUIET)
        assert inbox.count == 0


async def test_closed_session_does_not_break_admin_api(seeded_gateway):
    gw = seeded_gateway
    async with gw.mcp_session("coding") as c:
        await c.session.list_tools()
        await gw.wait_stream_open(c.session_id)
    # session 已關閉(SDK 用戶端 DELETE);登記可能仍在(WeakSet 尚未回收)
    elapsed = await _admin(gw, "POST", "/api/routes", {"name": "r3", "target": {"group": "coding"}}, 201)
    assert elapsed < 2.0
    pending = gw.call_in_loop(lambda: list(gw.app.state.mcp_events.pending))
    for _ in range(int(WAIT / 0.05)):
        if not pending or all(t.done() for t in pending):
            break
        await asyncio.sleep(0.05)
    assert all(t.done() for t in pending)


# ---- McpEvents 單元(假 session) --------------------------------------------------------------

class _Session:
    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour
        self.sent = 0

    async def send_tool_list_changed(self) -> None:
        if self.behaviour == "closed":
            raise anyio.ClosedResourceError
        if self.behaviour == "broken":
            raise anyio.BrokenResourceError
        if self.behaviour == "boom":
            raise RuntimeError("secret-ish detail")
        if self.behaviour == "hang":
            await asyncio.sleep(3600)
        self.sent += 1


async def test_changed_is_sync_and_tolerates_bad_sessions(monkeypatch, caplog):
    monkeypatch.setattr(events_mod, "NOTIFY_TIMEOUT", 0.2)
    ev = McpEvents()
    sessions = [_Session(b) for b in ("ok", "closed", "broken", "boom", "hang", "ok")]
    for s in sessions:
        ev.register(s)
        ev.register(s)                     # 重複登記不重複通知
    ev.register(None)
    start = time.monotonic()
    assert ev.changed() is None           # 同步、立即返回
    assert time.monotonic() - start < 0.05
    tasks = ev.pending
    assert len(tasks) == 6
    done, still = await asyncio.wait(tasks, timeout=2.0)
    assert not still                       # 卡住的 session 被 move_on_after 結束
    assert all(t.exception() is None for t in done)
    assert [s.sent for s in sessions] == [1, 0, 0, 0, 0, 1]
    assert "RuntimeError" in caplog.text and "secret-ish" not in caplog.text
    assert ev.pending == set()


async def test_changed_outside_event_loop_is_noop():
    ev = McpEvents()
    ev.register(_Session("ok"))
    await asyncio.to_thread(ev.changed)    # 另一個 thread、沒有事件迴圈
    assert ev.pending == set()


def test_sessions_are_weakly_held():
    import gc

    ev = McpEvents()
    s = _Session("ok")
    ev.register(s)
    assert ev.sessions == [s]
    del s
    gc.collect()
    assert ev.sessions == []

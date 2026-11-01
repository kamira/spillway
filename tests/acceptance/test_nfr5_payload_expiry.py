"""NFR-5:`usage_ledger` 不存 prompt 與回應;內容只存於 `usage_payloads`,過期後 1 小時內刪除。

量測方式依 spec 11:檢查 `usage_ledger` 欄位;保存天數設為 1 並推進系統時間 2 天,確認過期列已刪除。
這裡的「執行清理」由 app 的 RetentionScheduler 背景排程完成(不手動呼叫 run_once):輪詢間隔設為
0.05 秒(真實時間),推進 FakeClock 後等待排程的 `runs` 增加。
"""
from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import anyio
import pytest

from spillway.clock import to_iso

pytestmark = pytest.mark.anyio

LEDGER_COLUMNS = [
    "id", "ts", "route_name", "deployment_id", "effort", "tokens_in", "tokens_out", "cost_usd",
    "status", "slot", "fallback_depth", "latency_ms", "est_tokens_in", "est_tokens_out_p50",
    "est_tokens_out_p90", "est_cost_usd_p90", "error",
]
PROMPT = "NFR-5 檢查用的 prompt 文字"


async def test_nfr5_payload_expiry_and_ledger_has_no_content(open_spillway, fake_api):
    async with open_spillway(retention_poll_seconds=0.05) as h:
        c = h.client
        r = await c.post("/api/deployments", json={
            "id": "loc", "model_name": "loc", "kind": "local", "provider": "hosted_vllm",
            "model": "local-model", "params": {"api_base": fake_api.base_url + "/v1"}, "tags": ["t"]})
        assert r.status_code == 201, r.text
        r = await c.put("/api/settings/context", json={"context_retention_days": 1})
        assert r.status_code == 200 and r.json()["context_retention_days"] == 1

        r = await c.post("/v1/chat/completions",
                         json={"model": "local/loc", "messages": [{"role": "user", "content": PROMPT}]})
        assert r.status_code == 200, r.text
        ledger_id = (await c.get("/api/ledger")).json()["items"][0]["id"]
        assert (await c.get(f"/api/ledger/{ledger_id}/payload")).status_code == 200

        # 1) usage_ledger 欄位與 spec 2.3 一致,沒有內容欄位;列的值也不含 prompt 或回應
        with h.db() as conn:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(usage_ledger)")]
            values = [str(v) for row in conn.execute("SELECT * FROM usage_ledger") for v in tuple(row)]
            payload_cols = [row[1] for row in conn.execute("PRAGMA table_info(usage_payloads)")]
        assert cols == LEDGER_COLUMNS
        assert {"request_json", "response_text"} <= set(payload_cols)
        assert not any(PROMPT in v or "fake upstream ok" in v for v in values)

        # 2) 推進 2 天;由背景排程清理(每 retention_interval 一次、最壞延遲 ≤ 1 小時)
        runs_before = h.app.state.retention.runs
        h.clock.advance(days=2)
        deadline = time.monotonic() + 10
        while h.app.state.retention.runs == runs_before and time.monotonic() < deadline:
            await anyio.sleep(0.05)
        assert h.app.state.retention.runs > runs_before, "排程沒有在推進後執行清理"

        cutoff = to_iso(h.clock.now() - timedelta(days=1))
        with h.db() as conn:
            expired = conn.execute("SELECT COUNT(*) FROM usage_payloads WHERE ts < ?",
                                   (cutoff,)).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM usage_payloads").fetchone()[0]
            ledger_kept = conn.execute("SELECT COUNT(*) FROM usage_ledger WHERE id = ?",
                                       (ledger_id,)).fetchone()[0]
        assert expired == 0 and total == 0
        assert ledger_kept == 1                       # 帳本列保留,只刪內容
        r = await c.get(f"/api/ledger/{ledger_id}/payload")
        assert r.status_code == 410 and r.json()["error"]["code"] == "payload_expired"


# ---- CHG-20261101-02 T9(決策 14):過期時刻起 ≤ 1 小時刪除(固定相位,由排程本身輪詢觸發) ----

NFR5_POLL = 30          # 排程的 poll_seconds;每次輪詢 FakeClock 前進 poll


class _SteppedSleep:
    """取代 `spillway.app` 裡的 `asyncio.sleep`:排程每次輪詢的 sleep 停在這裡,測試呼叫 `step()`
    才讓它醒來並把 FakeClock 推進 `seconds`(= poll);醒來後由排程自己的 `_loop` 判斷 `due()` 並清理。"""

    def __init__(self, clock):
        self._clock = clock
        self._go = asyncio.Event()
        self._parked = asyncio.Event()

    async def sleep(self, seconds):
        self._parked.set()
        await self._go.wait()
        self._go.clear()
        self._clock.advance(seconds=seconds)

    async def wait_parked(self) -> None:
        await asyncio.wait_for(self._parked.wait(), timeout=5)

    async def step(self) -> None:
        """放行一次輪詢,等到排程處理完並回到下一次 sleep。"""
        await self.wait_parked()
        self._parked.clear()
        self._go.set()
        await self.wait_parked()


class _AppAsyncio:
    """只給 `spillway.app` 用的 asyncio 代理:`sleep` 換成 `_SteppedSleep.sleep`,其餘照舊。"""

    def __init__(self, stepped):
        self.sleep = stepped.sleep

    def __getattr__(self, name):
        return getattr(asyncio, name)


async def test_nfr5_payload_deleted_within_one_hour_of_expiry(open_spillway, fake_api, clock,
                                                              monkeypatch):
    import spillway.app as app_module

    stepped = _SteppedSleep(clock)
    monkeypatch.setattr(app_module, "asyncio", _AppAsyncio(stepped))
    async with open_spillway(retention_poll_seconds=NFR5_POLL) as h:
        sched = h.app.state.retention
        await stepped.wait_parked()                     # 排程已啟動並停在第一次輪詢的 sleep
        c = h.client
        r = await c.post("/api/deployments", json={
            "id": "loc", "model_name": "loc", "kind": "local", "provider": "hosted_vllm",
            "model": "local-model", "params": {"api_base": fake_api.base_url + "/v1"}, "tags": ["t"]})
        assert r.status_code == 201, r.text
        r = await c.put("/api/settings/context", json={"context_retention_days": 1})
        assert r.status_code == 200

        r = await c.post("/v1/chat/completions",
                         json={"model": "local/loc", "messages": [{"role": "user", "content": PROMPT}]})
        assert r.status_code == 200, r.text
        written_at = clock.now()                         # T
        ledger_id = (await c.get("/api/ledger")).json()["items"][0]["id"]
        expires_at = written_at + timedelta(days=1)      # T + 1 天

        def has_payload() -> bool:
            with h.db() as conn:
                return conn.execute("SELECT COUNT(*) FROM usage_payloads WHERE ledger_id = ?",
                                    (ledger_id,)).fetchone()[0] == 1

        assert has_payload()
        # 固定相位:排程在 T + 1 天前一個 poll 內(T + 1 天 − poll/2)剛清理過
        pinned = expires_at - timedelta(seconds=NFR5_POLL // 2)
        clock.set(pinned - timedelta(seconds=NFR5_POLL))
        runs = sched.runs
        await stepped.step()                             # 由排程本身的輪詢觸發這次清理
        assert clock.now() == pinned and sched.runs == runs + 1, "固定相位的清理沒有發生"
        assert clock.now() < expires_at and has_payload()   # T + 1 天前該列仍在

        # 之後 FakeClock 以 poll 步進,由排程本身輪詢觸發清理
        deleted_at = None
        while clock.now() < expires_at + timedelta(hours=1):
            await stepped.step()
            if deleted_at is None and not has_payload():
                deleted_at = clock.now()
        assert deleted_at is not None, "T + 1 天 + 1 小時時該列仍未刪除"
        assert expires_at < deleted_at <= expires_at + timedelta(hours=1)
        assert not has_payload()

        with h.db() as conn:                             # 帳本列保留
            assert conn.execute("SELECT COUNT(*) FROM usage_ledger WHERE id = ?",
                                (ledger_id,)).fetchone()[0] == 1
        r = await c.get(f"/api/ledger/{ledger_id}/payload")
        assert r.status_code == 410 and r.json()["error"]["code"] == "payload_expired"

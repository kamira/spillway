"""NFR-5:`usage_ledger` 不存 prompt 與回應;內容只存於 `usage_payloads`,過期後 1 小時內刪除。

量測方式依 spec 11:檢查 `usage_ledger` 欄位;保存天數設為 1 並推進系統時間 2 天,確認過期列已刪除。
這裡的「執行清理」由 app 的 RetentionScheduler 背景排程完成(不手動呼叫 run_once):輪詢間隔設為
0.05 秒(真實時間),推進 FakeClock 後等待排程的 `runs` 增加。
"""
from __future__ import annotations

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

"""NFR-5:`usage_ledger` 不存 prompt 與回應;內容只存於 `usage_payloads`,過期後 1 小時內刪除。

量測方式依 spec 11:檢查 `usage_ledger` 欄位;保存天數設為 1 並推進系統時間 2 天,確認過期列已刪除。
黑箱(CHG-20261107-03 T3;附錄 nfr5):serve 為子行程,以可控時鐘(`SPILLWAY_TEST_START_TIME`)啟動,
時間經 `POST /__test__/clock` 推進,保存排程的狀態經 `GET /__test__/retention` 觀察(spec 13.4.6)。
這裡的「執行清理」由 serve 的保存排程完成(不手動呼叫 `retention/run`):輪詢間隔
`SPILLWAY_TEST_RETENTION_POLL_SECONDS=0.05`(真實時間),推進時鐘後等待排程的 `runs` 增加。
「過期起 ≤ 1 小時刪除」測試(CHG-20261101-02 T9)以 STEPPED 模式(`SPILLWAY_TEST_RETENTION_STEPPED=1`)
取代轉換前對 `spillway.app` 的 `asyncio.sleep` monkeypatch:排程每一輪停在閘前,`POST /__test__/retention/tick`
推進時鐘 poll 秒並放行一輪,排程本身的 `_loop` 照一般輪詢做到期判斷並清理,回到閘前才回應。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from tests.blackbox.control import iso, parse_iso

LEDGER_COLUMNS = [
    "id", "ts", "route_name", "deployment_id", "effort", "tokens_in", "tokens_out", "cost_usd",
    "status", "slot", "fallback_depth", "latency_ms", "est_tokens_in", "est_tokens_out_p50",
    "est_tokens_out_p90", "est_cost_usd_p90", "error",
]
PROMPT = "NFR-5 檢查用的 prompt 文字"
# 可控時鐘的起點(轉換前 FakeClock 的預設值)
START = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)


def test_nfr5_payload_expiry_and_ledger_has_no_content(serve_factory, fake_api):
    h = serve_factory(start_time=START, retention_poll_seconds=0.05)
    with h.http() as c:
        r = c.post("/api/deployments", json={
            "id": "loc", "model_name": "loc", "kind": "local", "provider": "hosted_vllm",
            "model": "local-model", "params": {"api_base": fake_api.base_url + "/v1"}, "tags": ["t"]})
        assert r.status_code == 201, r.text
        r = c.put("/api/settings/context", json={"context_retention_days": 1})
        assert r.status_code == 200 and r.json()["context_retention_days"] == 1

        r = c.post("/v1/chat/completions",
                   json={"model": "local/loc", "messages": [{"role": "user", "content": PROMPT}]})
        assert r.status_code == 200, r.text
        ledger_id = c.get("/api/ledger").json()["items"][0]["id"]
        assert c.get(f"/api/ledger/{ledger_id}/payload").status_code == 200

        # 1) usage_ledger 欄位與 spec 2.3 一致,沒有內容欄位;列的值也不含 prompt 或回應
        with h.db() as conn:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(usage_ledger)")]
            values = [str(v) for row in conn.execute("SELECT * FROM usage_ledger") for v in tuple(row)]
            payload_cols = [row[1] for row in conn.execute("PRAGMA table_info(usage_payloads)")]
        assert cols == LEDGER_COLUMNS
        assert {"request_json", "response_text"} <= set(payload_cols)
        assert not any(PROMPT in v or "fake upstream ok" in v for v in values)

        # 2) 推進 2 天;由背景排程清理(每 retention_interval 一次、最壞延遲 ≤ 1 小時)
        runs_before = h.control.retention()["runs"]
        now = parse_iso(h.control.advance(int(timedelta(days=2).total_seconds())))
        deadline = time.monotonic() + 10
        while h.control.retention()["runs"] == runs_before and time.monotonic() < deadline:
            time.sleep(0.05)
        assert h.control.retention()["runs"] > runs_before, "排程沒有在推進後執行清理"

        cutoff = iso(now - timedelta(days=1))
        with h.db() as conn:
            expired = conn.execute("SELECT COUNT(*) FROM usage_payloads WHERE ts < ?",
                                   (cutoff,)).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM usage_payloads").fetchone()[0]
            ledger_kept = conn.execute("SELECT COUNT(*) FROM usage_ledger WHERE id = ?",
                                       (ledger_id,)).fetchone()[0]
        assert expired == 0 and total == 0
        assert ledger_kept == 1                       # 帳本列保留,只刪內容
        r = c.get(f"/api/ledger/{ledger_id}/payload")
        assert r.status_code == 410 and r.json()["error"]["code"] == "payload_expired"


# ---- CHG-20261101-02 T9(決策 14):過期時刻起 ≤ 1 小時刪除(固定相位,由排程本身輪詢觸發) ----

NFR5_POLL = 30          # 排程的 poll_seconds;每次輪詢可控時鐘前進 poll


class _SteppedSleep:
    """STEPPED 模式的保存排程(spec 13.4.6):排程每次輪詢停在閘前,測試呼叫 `step()` 才經
    `POST /__test__/retention/tick` 把時鐘推進 `seconds`(= poll)並放行一輪;排程自己的 `_loop`
    判斷 `due()` 並清理,回到閘前 tick 才回應。轉換前是把 `spillway.app` 的 `asyncio.sleep` 換成本類別。"""

    def __init__(self, control: Any, seconds: int = NFR5_POLL) -> None:
        self._control = control
        self._seconds = seconds
        self.last: dict[str, Any] | None = None

    def sleep(self, seconds: int) -> dict[str, Any]:
        """推進時鐘 `seconds` 秒並放行排程一輪(`retention/tick`);回傳 tick 的回應。"""
        self.last = self._control.tick(seconds)
        return self.last

    def wait_parked(self) -> None:
        """排程已啟動並停在閘前:放行一輪但不推進時鐘(`advance_seconds` 0;尚未到期,不清理)。"""
        self.sleep(0)

    def step(self) -> dict[str, Any]:
        """放行一次輪詢(時鐘前進 poll),等到排程處理完並回到下一次閘前。"""
        return self.sleep(self._seconds)


def test_nfr5_payload_deleted_within_one_hour_of_expiry(serve_factory, fake_api):
    # 本測試端到端驗證 NFR-5(過期起 ≤ 1 小時刪除),但提前量為 1 × poll 或 0 時同樣會通過,
    # 因此分辨不出新舊 `due()`;決策 14(2 × poll)只由 tests/integration/test_retention_schedule.py
    # 的 L1 抖動測試佐證。固定相位(E − poll/2)也不是最壞情況。
    h = serve_factory(start_time=START, retention_poll_seconds=NFR5_POLL, retention_stepped=True)
    ctl = h.control
    stepped = _SteppedSleep(ctl)
    stepped.wait_parked()                               # 排程已啟動並停在閘前(啟動時那一次清理已完成)
    assert stepped.last is not None and stepped.last["ran"] is False and stepped.last["runs"] >= 1
    with h.http() as c:
        r = c.post("/api/deployments", json={
            "id": "loc", "model_name": "loc", "kind": "local", "provider": "hosted_vllm",
            "model": "local-model", "params": {"api_base": fake_api.base_url + "/v1"}, "tags": ["t"]})
        assert r.status_code == 201, r.text
        r = c.put("/api/settings/context", json={"context_retention_days": 1})
        assert r.status_code == 200

        r = c.post("/v1/chat/completions",
                   json={"model": "local/loc", "messages": [{"role": "user", "content": PROMPT}]})
        assert r.status_code == 200, r.text
        written_at = ctl.now()                           # T
        ledger_id = c.get("/api/ledger").json()["items"][0]["id"]
        expires_at = written_at + timedelta(days=1)      # T + 1 天

        def has_payload() -> bool:
            with h.db() as conn:
                return conn.execute("SELECT COUNT(*) FROM usage_payloads WHERE ledger_id = ?",
                                    (ledger_id,)).fetchone()[0] == 1

        assert has_payload()
        # 固定相位:排程在 T + 1 天前一個 poll 內(T + 1 天 − poll/2)剛清理過
        pinned = expires_at - timedelta(seconds=NFR5_POLL // 2)   # 時鐘到秒(spec 13.3.2);poll 為偶數
        ctl.set_clock(pinned - timedelta(seconds=NFR5_POLL))
        runs = ctl.retention()["runs"]
        tick = stepped.step()                            # 由排程本身的輪詢觸發這次清理
        assert ctl.now() == pinned and tick["ran"] and tick["runs"] == runs + 1, "固定相位的清理沒有發生"
        assert ctl.now() < expires_at and has_payload()   # T + 1 天前該列仍在

        # 之後時鐘以 poll 步進,由排程本身輪詢觸發清理
        deleted_at = None
        while ctl.now() < expires_at + timedelta(hours=1):
            stepped.step()
            if deleted_at is None and not has_payload():
                deleted_at = ctl.now()
        assert deleted_at is not None, "T + 1 天 + 1 小時時該列仍未刪除"
        assert expires_at < deleted_at <= expires_at + timedelta(hours=1)
        assert not has_payload()

        with h.db() as conn:                             # 帳本列保留
            assert conn.execute("SELECT COUNT(*) FROM usage_ledger WHERE id = ?",
                                (ledger_id,)).fetchone()[0] == 1
        r = c.get(f"/api/ledger/{ledger_id}/payload")
        assert r.status_code == 410 and r.json()["error"]["code"] == "payload_expired"

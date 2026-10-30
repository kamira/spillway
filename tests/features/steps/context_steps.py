"""m2_context.feature 專用步驟(CHG-20261030-02 T15;AC-13、AC-14、AC-15、2.4.2、NFR-5)。"""
from __future__ import annotations

import json

from behave import step, use_step_matcher

from tests.features.support.gateway import DEFAULT_MESSAGES

use_step_matcher("re")

# spec 2.3 usage_ledger 的全部欄位;沒有任何 prompt / 回應內容欄位
LEDGER_COLUMNS = {
    "id", "ts", "route_name", "deployment_id", "effort", "tokens_in", "tokens_out", "cost_usd",
    "status", "slot", "fallback_depth", "latency_ms", "est_tokens_in", "est_tokens_out_p50",
    "est_tokens_out_p90", "est_cost_usd_p90", "error",
}


def _payload_rows(gw):
    with gw.db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM usage_payloads ORDER BY ledger_id")]


@step(r"usage_payloads 有 (\d+) 列")
def step_payload_rows(context, n):
    rows = _payload_rows(context.gw)
    assert len(rows) == int(n), rows


@step(r"usage_ledger 有 (\d+) 列")
def step_ledger_rows(context, n):
    assert context.gw.ledger_total() == int(n)


@step(r"該列含請求內容與回應內容")
def step_payload_content(context):
    gw = context.gw
    p = gw.ok(gw.get(f"/api/ledger/{gw.last_ledger_id}/payload"), 200)
    assert json.loads(p["request_json"]) == DEFAULT_MESSAGES, p
    assert p["response_text"] == "fake upstream ok", p
    assert p["bytes"] > 0


@step(r"usage_ledger 沒有任何 prompt 或回應內容欄位")
def step_ledger_no_content(context):
    gw = context.gw
    with gw.db() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(usage_ledger)")}
        values = [str(v) for row in conn.execute("SELECT * FROM usage_ledger") for v in tuple(row)]
    assert cols == LEDGER_COLUMNS, cols ^ LEDGER_COLUMNS
    prompt = DEFAULT_MESSAGES[0]["content"]
    assert not any(prompt in v or "fake upstream ok" in v for v in values), values


@step(r"context 保存天數為 (\d+)")
def step_retention_days(context, days):
    gw = context.gw
    out = gw.ok(gw.put("/api/settings/context", json={"context_retention_days": int(days)}), 200)
    assert out["context_retention_days"] == int(days)


@step(r"系統時間推進 (\d+) 天並執行清理")
def step_advance_cleanup(context, days):
    gw = context.gw
    assert _payload_rows(gw), "推進前沒有 payload 列"
    gw.clock.advance(days=int(days))
    deleted = gw.run_cleanup()
    assert deleted is not None and deleted >= 1, deleted


@step(r"該次的 usage_payloads 列已刪除")
def step_payload_deleted(context):
    gw = context.gw
    assert all(r["ledger_id"] != gw.last_ledger_id for r in _payload_rows(gw))
    # 帳本列保留(只刪內容)
    with gw.db() as conn:
        assert conn.execute("SELECT 1 FROM usage_ledger WHERE id = ?", (gw.last_ledger_id,)).fetchone()


@step(r'查詢該次 payload 回應狀態為 (\d+) 且錯誤碼為 "([^"]+)"')
def step_payload_status(context, status, code):
    gw = context.gw
    resp = gw.get(f"/api/ledger/{gw.last_ledger_id}/payload")
    assert resp.status_code == int(status), resp.text
    assert resp.json() == {"error": {"code": code, "message": resp.json()["error"]["message"]}}
    assert resp.json()["error"]["code"] == code


@step(r'儲存的請求內容不含 "([^"]+)"')
def step_payload_no_secret(context, secret):
    gw = context.gw
    p = gw.ok(gw.get(f"/api/ledger/{gw.last_ledger_id}/payload"), 200)
    assert secret not in p["request_json"] and secret not in (p["response_text"] or ""), p
    rows = _payload_rows(gw)
    assert rows and all(secret not in json.dumps(r, ensure_ascii=False) for r in rows)
    # 遮罩只取代金鑰,其餘 prompt 文字仍在
    content = json.loads(p["request_json"])[0]["content"]
    assert content.startswith("請用 ") and content.endswith(" 呼叫") and secret not in content, content


@step(r"該列 redacted 為 1")
def step_payload_redacted(context):
    gw = context.gw
    with gw.db() as conn:
        v = conn.execute("SELECT redacted FROM usage_payloads WHERE ledger_id = ?",
                         (gw.last_ledger_id,)).fetchone()[0]
    assert v == 1
    assert gw.ok(gw.get(f"/api/ledger/{gw.last_ledger_id}/payload"), 200)["redacted"] is True

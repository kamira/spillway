"""NFR-4 預先量測(不計入 M2 閘門;CHG-20261030-02 A14、安全影響 (e)(f)):

以已知金鑰 `sk-test-0000`(`SPILLWAY_TEST_KEY`,成員以 `env:` 參照)跑完整流程後,以該字串搜尋:
- `SPILLWAY_HOME` 下全部檔案的位元組(含 `spillway.db-wal`、`spillway.db-shm`;全程保持一條連線讓 WAL
  留在磁碟,app 開著時掃一次,關閉、checkpoint 後再掃一次);
- caplog(以 DEBUG 擷取全部 logger 的訊息、參數與例外文字):INFO 以上全程 0 命中;DEBUG 除了
  「prompt 含金鑰」那次請求期間(LiteLLM 自己的 DEBUG 日誌會印 prompt 原文,只記錄不判定)之外也 0 命中,
  即憑證值在任何等級都不出現;
- `artifacts/m2/` 下全部檔案;
- 另加:本流程中每個 HTTP 回應的 body 與 header。
命中數必須為 0。正式的 NFR-4(含匯出檔)屬 M4 閘門。

流程:管理 API 建 openai、anthropic 兩個 api 成員與一個 local 成員、一個 route;成功請求;prompt 含金鑰的
請求;假上游 `echo_auth`(錯誤訊息回顯 Authorization / x-api-key 值)下的請求(全部失敗 → 503)與
`/api/deployments/{id}/test`;429 冷卻;dry-run、estimate、dashboard、ledger、每列 payload;
把金鑰塞進 params、credential_ref、成員 id 的寫入(422/404,不回顯)。
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from spillway.store.db import connect
from tests.acceptance.results import ARTIFACTS, record_result

pytestmark = pytest.mark.anyio

KEY = "sk-test-0000"
KEY_BYTES = KEY.encode()


def _hits_in_tree(root: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = p.read_bytes().count(KEY_BYTES)
    return out


async def test_nfr4_known_key_prescan(open_spillway, fake_api, test_key_env, caplog, artifacts_dir):
    caplog.set_level(logging.DEBUG)
    responses = []

    async with open_spillway() as h:
        c = h.client
        # 全程保持一條連線,讓 WAL 不在最後一條連線關閉時被 checkpoint 並刪除:掃描時 -wal、-shm
        # 仍在,且含本流程寫入的頁面
        keeper = connect(h.db_path)

        async def call(method: str, url: str, **kw):
            r = await c.request(method, url, **kw)
            responses.append(r)
            return r

        base = fake_api.base_url
        members = [
            {"id": "oa", "model_name": "oa", "kind": "api", "provider": "openai", "model": "gpt-4o-mini",
             "params": {"api_base": base + "/v1"}, "credential_ref": "env:SPILLWAY_TEST_KEY",
             "tags": ["scan"]},
            {"id": "an", "model_name": "an", "kind": "api", "provider": "anthropic",
             "model": "claude-sonnet-4-5", "params": {"api_base": base},
             "credential_ref": "env:SPILLWAY_TEST_KEY", "tags": ["scan"]},
            {"id": "lo", "model_name": "lo", "kind": "local", "provider": "hosted_vllm",
             "model": "local-model", "params": {"api_base": base + "/v1"}, "tags": ["scan"]},
        ]
        for m in members:
            assert (await call("POST", "/api/deployments", json=m)).status_code == 201
        r = await call("POST", "/api/routes", json={"name": "scan",
                                                    "target": {"tags": {"any": ["scan"]}}})
        assert r.status_code == 201, r.text
        r = await call("PUT", "/api/routes/scan/order", json={"slots": [
            [{"deployment": "oa"}], [{"deployment": "an"}], [{"deployment": "lo"}]]})
        assert r.status_code == 200, r.text

        msgs = [{"role": "user", "content": "hello"}]
        assert (await call("POST", "/v1/chat/completions",
                           json={"model": "scan", "messages": msgs})).status_code == 200
        # 使用者把金鑰貼進 prompt(AC-15)。這段期間的 caplog 紀錄另外標記:LiteLLM 自己的 DEBUG 日誌
        # 會印出 prompt 原文(見下方 debug_prompt_hits),不是憑證外洩
        prompt_window_start = len(caplog.records)
        r = await call("POST", "/v1/chat/completions", json={"model": "scan", "messages": [
            {"role": "user", "content": f"請用 {KEY} 呼叫,Authorization: Bearer {KEY}"}]})
        assert r.status_code == 200
        prompt_window = range(prompt_window_start, len(caplog.records))

        # 上游回顯憑證:三個成員全部失敗 → 503;錯誤訊息、帳本 error、payload 都必須已遮罩
        fake_api.set_mode("*", "echo_auth")
        r = await call("POST", "/v1/chat/completions", json={"model": "scan", "messages": msgs})
        assert r.status_code == 503, r.text
        for dep_id in ("oa", "an", "lo"):
            assert (await call("POST", f"/api/deployments/{dep_id}/test")).status_code == 200
        r = await call("POST", "/v1/chat/completions", json={"model": "local/lo", "messages": msgs})
        assert r.status_code == 503

        # 429 → 冷卻;其餘成員接手
        fake_api.reset()
        fake_api.set_mode("gpt-4o-mini", "http_429")
        r = await call("POST", "/v1/chat/completions", json={"model": "scan", "messages": msgs})
        assert r.status_code == 200 and r.headers["X-Spillway-Served-By"] == "an"
        fake_api.reset()

        # 唯讀端點
        assert (await call("POST", "/api/routes/scan/dry-run", json={"messages": msgs})).status_code == 200
        assert (await call("POST", "/api/estimate", json={"model": "scan", "messages": msgs})).status_code == 200
        assert (await call("GET", "/api/dashboard")).status_code == 200
        assert (await call("GET", "/api/adapters")).status_code == 200
        assert (await call("GET", "/api/deployments")).status_code == 200
        ledger = (await call("GET", "/api/ledger", params={"page_size": 500})).json()["items"]
        assert len(ledger) >= 8
        for row in ledger:
            await call("GET", f"/api/ledger/{row['id']}/payload")

        # 把金鑰塞進寫入 → 422 / 404,不回顯
        bad = {**members[0], "id": "oa2", "params": {"api_base": base + "/v1", "x": KEY}}
        assert (await call("POST", "/api/deployments", json=bad)).status_code == 422
        bad = {**members[0], "id": "oa3", "credential_ref": KEY}
        assert (await call("POST", "/api/deployments", json=bad)).status_code == 422
        assert (await call("GET", f"/api/deployments/{KEY}")).status_code == 404
        assert (await call("POST", "/v1/chat/completions",
                           json={"model": KEY, "messages": msgs})).status_code == 404

        # 前提:真的有寫入過含金鑰的內容(否則掃描 0 沒有意義)
        with h.db() as conn:
            redacted_rows = conn.execute("SELECT COUNT(*) FROM usage_payloads WHERE redacted = 1").fetchone()[0]
            ledger_errors = conn.execute("SELECT COUNT(*) FROM usage_ledger "
                                         "WHERE error LIKE '%[REDACTED]%'").fetchone()[0]
        assert redacted_rows >= 1, redacted_rows     # prompt 含金鑰的那一列
        assert ledger_errors >= 2, ledger_errors     # echo_auth 的錯誤訊息確實含過金鑰並被遮罩
        home_open = _hits_in_tree(h.home)
        wal = h.db_path.with_name(h.db_path.name + "-wal")
        assert wal.stat().st_size > 0 and h.db_path.with_name(h.db_path.name + "-shm").exists(), \
            sorted(home_open)
        keeper.close()

    home_closed = _hits_in_tree(h.home)
    def rec_hits(rec: logging.LogRecord) -> int:
        return f"{rec.getMessage()} {rec.args!r} {rec.exc_text or ''}".count(KEY)

    # 閘門 1:INFO 以上(serve 的 uvicorn 為 info、root 預設 WARNING)全程 0 命中
    log_hits = sum(rec_hits(r) for r in caplog.records if r.levelno >= logging.INFO)
    # 閘門 2:DEBUG 也算,但排除「prompt 含金鑰」那次請求期間的紀錄 → 憑證本身在任何等級都不出現
    debug_cred_hits = sum(rec_hits(r) for i, r in enumerate(caplog.records)
                          if r.levelno < logging.INFO and i not in prompt_window)
    # 只記錄:LiteLLM DEBUG 日誌印出 prompt 原文(使用者貼上的字串);正式執行不開 DEBUG
    debug_prompt_hits = sum(rec_hits(caplog.records[i]) for i in prompt_window
                            if caplog.records[i].levelno < logging.INFO)
    resp_hits = sum(r.content.count(KEY_BYTES) + str(dict(r.headers)).count(KEY) for r in responses)
    artifact_hits = _hits_in_tree(ARTIFACTS)

    result = {
        "spillway_home_open": sum(home_open.values()),
        "spillway_home_closed": sum(home_closed.values()),
        "spillway_home_files": sorted(set(home_open) | set(home_closed)),
        "caplog_info_and_above": log_hits,
        "caplog_debug_outside_prompt_request": debug_cred_hits,
        "caplog_debug_prompt_echo_by_litellm": debug_prompt_hits,
        "caplog_records": len(caplog.records),
        "artifacts_m2": sum(artifact_hits.values()),
        "artifacts_m2_files": sorted(artifact_hits),
        "http_responses": resp_hits,
        "http_responses_checked": len(responses),
        "payload_rows_redacted": redacted_rows,
        "ledger_rows_with_masked_error": ledger_errors,
        "note": "預先量測,不計入 M2 閘門",
    }
    record_result("nfr4_prescan", result)
    assert {k: v for k, v in home_open.items() if v} == {}
    assert {k: v for k, v in home_closed.items() if v} == {}
    assert log_hits == 0
    assert debug_cred_hits == 0
    assert {k: v for k, v in artifact_hits.items() if v} == {}
    assert resp_hits == 0

"""NFR-4 預先量測(不計入 M2 閘門;CHG-20261030-02 A14、安全影響 (e)(f)):

以已知金鑰 `sk-test-0000`(`SPILLWAY_TEST_KEY`,成員以 `env:` 參照)跑完整流程後,以該字串搜尋:
- `SPILLWAY_HOME` 下全部檔案的位元組(含 `spillway.db-wal`、`spillway.db-shm`;全程保持一條連線讓 WAL
  留在磁碟,app 開著時掃一次,關閉、checkpoint 後再掃一次);
- caplog(以 DEBUG 擷取全部 logger 的訊息、參數與例外文字):INFO 以上全程 0 命中;DEBUG 也全程 0 命中,
  即憑證值在任何等級都不出現。「prompt 含金鑰」那次請求貼的是另一個 `sk-` 字串(`PROMPT_KEY`,與憑證不同;
  末端審查 S5),LiteLLM 自己的 DEBUG 日誌印出它只記錄不判定,憑證在該期間仍照樣掃描;
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
# 使用者貼進 prompt 的金鑰字串,刻意與憑證 KEY 不同(末端審查 S5):prompt 期間的排除窗只排除它,
# 憑證 KEY 在任何時段、任何等級的日誌都仍須 0 命中
PROMPT_KEY = "sk-pasted-prompt-5555"


def _hits_in_tree(root: Path, needle: bytes = KEY_BYTES) -> dict[str, int]:
    out: dict[str, int] = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = p.read_bytes().count(needle)
    return out


async def test_nfr4_known_key_prescan(open_spillway, fake_api, test_key_env, caplog, artifacts_dir):
    caplog.set_level(logging.DEBUG)
    assert test_key_env == KEY and PROMPT_KEY != KEY
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
            {"role": "user", "content": f"請用 {PROMPT_KEY} 呼叫,Authorization: Bearer {PROMPT_KEY}"}]})
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
        prompt_home_open = _hits_in_tree(h.home, PROMPT_KEY.encode())
        wal = h.db_path.with_name(h.db_path.name + "-wal")
        assert wal.stat().st_size > 0 and h.db_path.with_name(h.db_path.name + "-shm").exists(), \
            sorted(home_open)
        keeper.close()

    home_closed = _hits_in_tree(h.home)
    prompt_home_closed = _hits_in_tree(h.home, PROMPT_KEY.encode())

    def rec_hits(rec: logging.LogRecord, needle: str = KEY) -> int:
        return f"{rec.getMessage()} {rec.args!r} {rec.exc_text or ''}".count(needle)

    # 閘門 1:INFO 以上(serve 的 uvicorn 為 info、root 預設 WARNING)全程 0 命中
    log_hits = sum(rec_hits(r) for r in caplog.records if r.levelno >= logging.INFO)
    # 閘門 2:DEBUG 也算。prompt 用的是另一個字串,所以「prompt 含金鑰」那次請求期間也照樣掃描憑證
    # → 憑證本身在任何時段、任何等級都不出現
    debug_cred_hits = sum(rec_hits(r) for r in caplog.records if r.levelno < logging.INFO)
    debug_cred_hits_in_prompt_window = sum(rec_hits(caplog.records[i]) for i in prompt_window
                                           if caplog.records[i].levelno < logging.INFO)
    # 只記錄:LiteLLM DEBUG 日誌印出 prompt 原文(使用者貼上的 PROMPT_KEY);正式執行不開 DEBUG
    debug_prompt_hits = sum(rec_hits(caplog.records[i], PROMPT_KEY) for i in prompt_window
                            if caplog.records[i].levelno < logging.INFO)
    prompt_info_hits = sum(rec_hits(r, PROMPT_KEY) for r in caplog.records if r.levelno >= logging.INFO)
    resp_hits = sum(r.content.count(KEY_BYTES) + str(dict(r.headers)).count(KEY) for r in responses)
    artifact_hits = _hits_in_tree(ARTIFACTS)

    result = {
        "spillway_home_open": sum(home_open.values()),
        "spillway_home_closed": sum(home_closed.values()),
        "spillway_home_files": sorted(set(home_open) | set(home_closed)),
        "caplog_info_and_above": log_hits,
        "caplog_debug_all": debug_cred_hits,
        "caplog_debug_credential_in_prompt_request": debug_cred_hits_in_prompt_window,
        "caplog_debug_prompt_echo_by_litellm": debug_prompt_hits,
        "prompt_key_in_spillway_home": sum(prompt_home_open.values()) + sum(prompt_home_closed.values()),
        "prompt_key_info_and_above": prompt_info_hits,
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
    assert debug_cred_hits == 0 and debug_cred_hits_in_prompt_window == 0
    # 貼進 prompt 的字串由 `sk-` 樣式規則遮罩後才落地(AC-15)
    assert {k: v for k, v in prompt_home_open.items() if v} == {}
    assert {k: v for k, v in prompt_home_closed.items() if v} == {}
    assert prompt_info_hits == 0
    assert {k: v for k, v in artifact_hits.items() if v} == {}
    assert resp_hits == 0

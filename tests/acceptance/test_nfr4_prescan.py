"""NFR-4 預先量測(不計入 M2 閘門;CHG-20261030-02 A14、安全影響 (e)(f)):

以已知金鑰 `sk-test-0000`(`SPILLWAY_TEST_KEY`,成員以 `env:` 參照)跑完整流程後,以該字串搜尋:
- `SPILLWAY_HOME` 下全部檔案的位元組(含 `spillway.db-wal`、`spillway.db-shm`;全程保持一條唯讀連線讓 WAL
  留在磁碟,serve 開著時掃一次,停止、checkpoint 後再掃一次);
- serve 的日誌:黑箱(CHG-20261107-03 決策 10 / AM-11)以 `SPILLWAY_TEST_LOG_LEVEL` 讓 serve 子行程把日誌寫成
  stderr 的 `SPILLWAY-LOG <等級> <logger> <訊息>` 行(spec 14.3.5;續行 `SPILLWAY-LOG-CONT `),依該行解析等級。
  流程跑兩次、各自一個 serve 與資料目錄:`INFO` 那次全程 0 命中;`DEBUG` 那次憑證在任何等級也 0 命中。
  「prompt 含金鑰」那次請求貼的是另一個 `sk-` 字串(`PROMPT_KEY`,與憑證不同;末端審查 S5),LiteLLM 自己的
  DEBUG 日誌印出它只記錄不判定(豁免窗口,實作相依;Python 版的 LiteLLM logger 有 INFO 下限,預期為 0 筆),
  憑證在該期間仍照樣掃描。窗口以送出請求前後的 stderr 位移界定,等 stderr 排乾後才掃描;`TOKEN_NOTICE` 行
  以精確前綴排除(token 不是憑證,也不經日誌);
- 前提(沒有它會「沒有東西可掃也通過」):`DEBUG` 那次全程至少一行 `SPILLWAY-LOG DEBUG`;兩次的請求窗口內都至少
  一行 logger 名稱以 `LiteLLM` 開頭的 `SPILLWAY-LOG` 行(任何等級;logger 名稱屬實作相依,見
  `tests/blackbox/impl_dependent.toml`);
- `artifacts/m2/` 下全部檔案;
- 另加:本流程中每個 HTTP 回應的 body 與 header。
命中數必須為 0。正式的 NFR-4(含匯出檔)屬 M4 閘門。

流程:管理 API 建 openai、anthropic 兩個 api 成員與一個 local 成員、一個 route;成功請求;prompt 含金鑰的
請求;假上游 `echo_auth`(錯誤訊息回顯 Authorization / x-api-key 值)下的請求(全部失敗 → 503)與
`/api/deployments/{id}/test`;429 冷卻;dry-run、estimate、dashboard、ledger、每列 payload;
把金鑰塞進 params、credential_ref、成員 id 的寫入(422/404,不回顯)。
"""
from __future__ import annotations

import contextlib
from pathlib import Path

import httpx

from tests.acceptance.results import ARTIFACTS, record_result
from tests.blackbox.harness import LOG_CONT_PREFIX, LOG_PREFIX, TOKEN_NOTICE

KEY = "sk-test-0000"
KEY_BYTES = KEY.encode()
# 使用者貼進 prompt 的金鑰字串,刻意與憑證 KEY 不同(末端審查 S5):prompt 期間的排除窗只排除它,
# 憑證 KEY 在任何時段、任何等級的日誌都仍須 0 命中
PROMPT_KEY = "sk-pasted-prompt-5555"
LEVELS = ("INFO", "DEBUG")
BELOW_INFO = {"DEBUG"}
LITELLM_LOGGER_PREFIX = "LiteLLM"     # 實作相依(impl_dependent.toml)


def _hits_in_tree(root: Path, needle: bytes = KEY_BYTES) -> dict[str, int]:
    out: dict[str, int] = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = p.read_bytes().count(needle)
    return out


def _log_records(data: bytes) -> list[tuple[str, str, str]]:
    """stderr 位元組 → `SPILLWAY-LOG` 紀錄 `(等級, logger 名稱, 全文含續行)`;其他行(含 `TOKEN_NOTICE`)略過。"""
    out: list[tuple[str, str, str]] = []
    for line in data.decode("utf-8", errors="replace").split("\n"):
        if line.startswith(TOKEN_NOTICE):
            continue
        if line.startswith(LOG_PREFIX):
            parts = line.split(" ", 3)
            out.append((parts[1], parts[2] if len(parts) > 2 else "", line))
        elif line.startswith(LOG_CONT_PREFIX) and out:
            level, name, text = out[-1]
            out[-1] = (level, name, text + "\n" + line)
    return out


def test_nfr4_known_key_prescan(serve_factory, fake_api, test_key_env, artifacts_dir, tmp_path):
    assert test_key_env == KEY and PROMPT_KEY != KEY
    responses: list[httpx.Response] = []
    runs: dict[str, dict] = {}

    for level in LEVELS:
        fake_api.reset()
        h = serve_factory(home=tmp_path / f"home-{level.lower()}", log_level=level)
        keeper_stack = contextlib.ExitStack()
        # 全程保持一條(唯讀)連線,讓 WAL 不在最後一條連線關閉時被 checkpoint 並刪除:掃描時 -wal、-shm
        # 仍在,且含本流程寫入的頁面
        keeper = keeper_stack.enter_context(h.db())
        keeper.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        c = h.http()

        def call(method: str, url: str, **kw):
            r = c.request(method, url, **kw)
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
            assert call("POST", "/api/deployments", json=m).status_code == 201
        r = call("POST", "/api/routes", json={"name": "scan", "target": {"tags": {"any": ["scan"]}}})
        assert r.status_code == 201, r.text
        r = call("PUT", "/api/routes/scan/order", json={"slots": [
            [{"deployment": "oa"}], [{"deployment": "an"}], [{"deployment": "lo"}]]})
        assert r.status_code == 200, r.text

        h.drain_stderr()
        request_window_start = h.stderr_offset()
        msgs = [{"role": "user", "content": "hello"}]
        assert call("POST", "/v1/chat/completions", json={"model": "scan", "messages": msgs}).status_code == 200
        # 使用者把金鑰貼進 prompt(AC-15)。這段期間的日誌另外標記:LiteLLM 自己的 DEBUG 日誌
        # 會印出 prompt 原文(見下方 debug_prompt_hits),不是憑證外洩
        h.drain_stderr()
        prompt_window_start = h.stderr_offset()
        r = call("POST", "/v1/chat/completions", json={"model": "scan", "messages": [
            {"role": "user", "content": f"請用 {PROMPT_KEY} 呼叫,Authorization: Bearer {PROMPT_KEY}"}]})
        assert r.status_code == 200
        h.drain_stderr()
        prompt_window_end = h.stderr_offset()

        # 上游回顯憑證:三個成員全部失敗 → 503;錯誤訊息、帳本 error、payload 都必須已遮罩
        fake_api.set_mode("*", "echo_auth")
        r = call("POST", "/v1/chat/completions", json={"model": "scan", "messages": msgs})
        assert r.status_code == 503, r.text
        for dep_id in ("oa", "an", "lo"):
            assert call("POST", f"/api/deployments/{dep_id}/test").status_code == 200
        r = call("POST", "/v1/chat/completions", json={"model": "local/lo", "messages": msgs})
        assert r.status_code == 503

        # 429 → 冷卻;其餘成員接手
        fake_api.reset()
        fake_api.set_mode("gpt-4o-mini", "http_429")
        r = call("POST", "/v1/chat/completions", json={"model": "scan", "messages": msgs})
        assert r.status_code == 200 and r.headers["X-Spillway-Served-By"] == "an"
        fake_api.reset()

        # 唯讀端點
        assert call("POST", "/api/routes/scan/dry-run", json={"messages": msgs}).status_code == 200
        assert call("POST", "/api/estimate", json={"model": "scan", "messages": msgs}).status_code == 200
        assert call("GET", "/api/dashboard").status_code == 200
        assert call("GET", "/api/adapters").status_code == 200
        assert call("GET", "/api/deployments").status_code == 200
        ledger = call("GET", "/api/ledger", params={"page_size": 500}).json()["items"]
        assert len(ledger) >= 8
        for row in ledger:
            call("GET", f"/api/ledger/{row['id']}/payload")

        # 把金鑰塞進寫入 → 422 / 404,不回顯
        bad = {**members[0], "id": "oa2", "params": {"api_base": base + "/v1", "x": KEY}}
        assert call("POST", "/api/deployments", json=bad).status_code == 422
        bad = {**members[0], "id": "oa3", "credential_ref": KEY}
        assert call("POST", "/api/deployments", json=bad).status_code == 422
        assert call("GET", f"/api/deployments/{KEY}").status_code == 404
        assert call("POST", "/v1/chat/completions", json={"model": KEY, "messages": msgs}).status_code == 404
        h.drain_stderr()
        request_window_end = h.stderr_offset()
        c.close()

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
        keeper_stack.close()
        h.stop()
        stderr = h.stderr_bytes()
        runs[level] = {
            "home": h.home, "home_open": home_open, "prompt_home_open": prompt_home_open,
            "home_closed": _hits_in_tree(h.home), "prompt_home_closed": _hits_in_tree(h.home, PROMPT_KEY.encode()),
            "records": _log_records(stderr),
            "request_window": _log_records(stderr[request_window_start:request_window_end]),
            "prompt_window": _log_records(stderr[prompt_window_start:prompt_window_end]),
            "redacted_rows": redacted_rows, "ledger_errors": ledger_errors,
        }

    def rec_hits(rec: tuple[str, str, str], needle: str = KEY) -> int:
        return rec[2].count(needle)

    info, debug = runs["INFO"], runs["DEBUG"]
    # 前提(決策 10):DEBUG 那次真的有 DEBUG 行;請求窗口內真的有 LiteLLM 的日誌行(否則沒有東西可掃)
    debug_lines = sum(1 for rec in debug["records"] if rec[0] == "DEBUG")
    litellm_in_window = {lv: sum(1 for rec in runs[lv]["request_window"] if rec[1].startswith(LITELLM_LOGGER_PREFIX))
                         for lv in LEVELS}
    assert debug_lines >= 1, debug_lines
    assert all(n >= 1 for n in litellm_in_window.values()), litellm_in_window

    # 閘門 1:INFO 以上(兩次啟動)全程 0 命中;INFO 那次的設定等級下不會有 DEBUG 行
    log_hits = sum(rec_hits(rec) for lv in LEVELS for rec in runs[lv]["records"] if rec[0] not in BELOW_INFO)
    assert not any(rec[0] in BELOW_INFO for rec in info["records"])
    # 閘門 2:DEBUG 也算。prompt 用的是另一個字串,所以「prompt 含金鑰」那次請求期間也照樣掃描憑證
    # → 憑證本身在任何時段、任何等級都不出現
    debug_cred_hits = sum(rec_hits(rec) for rec in debug["records"] if rec[0] in BELOW_INFO)
    debug_cred_hits_in_prompt_window = sum(rec_hits(rec) for rec in debug["prompt_window"] if rec[0] in BELOW_INFO)
    # 只記錄(豁免窗口,實作相依):LiteLLM DEBUG 日誌印出 prompt 原文(使用者貼上的 PROMPT_KEY);正式執行不開 DEBUG
    debug_prompt_hits = sum(rec_hits(rec, PROMPT_KEY) for rec in debug["prompt_window"]
                            if rec[0] in BELOW_INFO and rec[1].startswith(LITELLM_LOGGER_PREFIX))
    debug_prompt_hits_not_litellm = sum(rec_hits(rec, PROMPT_KEY) for rec in debug["records"]
                                        if rec[0] in BELOW_INFO and not rec[1].startswith(LITELLM_LOGGER_PREFIX))
    prompt_info_hits = sum(rec_hits(rec, PROMPT_KEY) for lv in LEVELS for rec in runs[lv]["records"]
                           if rec[0] not in BELOW_INFO)
    resp_hits = sum(r.content.count(KEY_BYTES) + str(dict(r.headers)).count(KEY) for r in responses)
    artifact_hits = _hits_in_tree(ARTIFACTS)
    home_open = {f"{lv}:{k}": v for lv in LEVELS for k, v in runs[lv]["home_open"].items()}
    home_closed = {f"{lv}:{k}": v for lv in LEVELS for k, v in runs[lv]["home_closed"].items()}
    prompt_home_open = {f"{lv}:{k}": v for lv in LEVELS for k, v in runs[lv]["prompt_home_open"].items()}
    prompt_home_closed = {f"{lv}:{k}": v for lv in LEVELS for k, v in runs[lv]["prompt_home_closed"].items()}

    result = {
        "spillway_home_open": sum(home_open.values()),
        "spillway_home_closed": sum(home_closed.values()),
        "spillway_home_files": sorted(set(home_open) | set(home_closed)),
        "log_source": "serve stderr 的 SPILLWAY-LOG 行(SPILLWAY_TEST_LOG_LEVEL=INFO 與 DEBUG 各一次啟動)",
        "log_info_and_above": log_hits,
        "log_debug_all": debug_cred_hits,
        "log_debug_credential_in_prompt_request": debug_cred_hits_in_prompt_window,
        "log_debug_prompt_echo_by_litellm": debug_prompt_hits,
        "log_debug_lines": debug_lines,
        "log_litellm_lines_in_request_window": litellm_in_window,
        "prompt_key_in_spillway_home": sum(prompt_home_open.values()) + sum(prompt_home_closed.values()),
        "prompt_key_info_and_above": prompt_info_hits,
        "prompt_key_debug_outside_litellm": debug_prompt_hits_not_litellm,
        "log_records": {lv: len(runs[lv]["records"]) for lv in LEVELS},
        "artifacts_m2": sum(artifact_hits.values()),
        "artifacts_m2_files": sorted(artifact_hits),
        "http_responses": resp_hits,
        "http_responses_checked": len(responses),
        "payload_rows_redacted": {lv: runs[lv]["redacted_rows"] for lv in LEVELS},
        "ledger_rows_with_masked_error": {lv: runs[lv]["ledger_errors"] for lv in LEVELS},
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

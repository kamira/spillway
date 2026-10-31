"""L4 操作驗收(CHG-20261030-02「### Acceptance operation」;macOS)。

operate:暫存 `SPILLWAY_HOME`;子行程啟動假上游(`python -m tests.fakes.api_upstream --port 0`)與
`python -m spillway serve --port <隨機埠>`;經 HTTP 管理 API 建兩個 api 成員(slot 1、slot 2)與一個
local 成員(provider `hosted_vllm`,`api_base` 指向假上游)、一個 target 只含這三者的 route;以假上游
控制端點讓 slot 1 成員回 429,送一次請求使其進入冷卻,再送一次;`model=local/<id>`;含 `sk-test-0000`
的 prompt;dry-run;`stream: true`;`lsof -nP -iTCP -sTCP:LISTEN`。

observe(逐項斷言):冷卻後那次的 Served-By 為 slot 2 成員、Slot 為 2,假上游對 slot 1 成員沒有收到
第二次請求;local 直呼的 Slot 為 `direct`、帳本 `slot` 為 NULL;payload 不含 `sk-test-0000`;dry-run 前後
帳本列數不變;`stream: true` 回 400 `streaming_unsupported`;只有 127.0.0.1:<埠> 在監聽。

產物:`artifacts/m2/http-log.jsonl`——每行一筆對閘道的 HTTP 請求,只含 method、path(不含 query)、
status、耗時(ms),整行寫入前經 Redactor。測試結束(含失敗)終止全部子行程。
各項觀察結果另記入 `artifacts/m2/acceptance-results.json` 的 `operation`。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from spillway.redact import Redactor
from tests.acceptance import procs
from tests.acceptance.results import ARTIFACTS, record_result

pytestmark = pytest.mark.skipif(not procs.HAS_LSOF, reason="需要 lsof(macOS 驗收平台)")

KEY = procs.PROMPT_KEY          # prompt 裡的金鑰字串,只能靠 `sk-` 樣式規則遮罩
CRED = procs.TEST_KEY           # 憑證值,與 KEY 不同(M28)
assert KEY != CRED
SLOT1, SLOT2, LOCAL = "op-api-1", "op-api-2", "op-local"
MODEL1, MODEL2 = "gpt-4o-mini", "gpt-4.1-mini"
ROUTE = "op-route"
HTTP_LOG = ARTIFACTS / "http-log.jsonl"


class LoggedClient:
    """對閘道的 httpx client;每次請求記 method、path、status、耗時(經 Redactor 後寫入)。"""

    def __init__(self, base_url: str) -> None:
        self.client = httpx.Client(base_url=base_url, timeout=60)
        self.lines: list[str] = []
        self._redactor = Redactor()

    def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        t0 = time.perf_counter()
        r = self.client.request(method, path, **kw)
        ms = round((time.perf_counter() - t0) * 1000, 2)
        line = json.dumps({"method": method, "path": r.request.url.path, "status": r.status_code,
                           "elapsed_ms": ms}, ensure_ascii=False)
        self.lines.append(self._redactor.redact(line)[0])
        return r

    def chat(self, model: str, content: str = "hello", **extra: Any) -> httpx.Response:
        return self.request("POST", "/v1/chat/completions",
                            json={"model": model, "messages": [{"role": "user", "content": content}],
                                  **extra})

    def write_log(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(ln + "\n" for ln in self.lines), encoding="utf-8")

    def close(self) -> None:
        self.client.close()


def _member(dep_id: str, kind: str, provider: str, model: str, fake_base: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": dep_id, "model_name": dep_id, "kind": kind, "provider": provider, "model": model,
        "params": {"api_base": fake_base + "/v1"}, "tags": ["op"],
        "capabilities": {"ctx": 32768, "tools": False, "vision": False, "efforts": []},
    }
    if kind == "api":
        body["credential_ref"] = "env:SPILLWAY_TEST_KEY"
    return body


def _fake_requests(fake_base: str, model: str) -> int:
    rows = httpx.get(fake_base + "/_fake/requests", timeout=10).json()
    return sum(1 for r in rows if r["path"] == "/v1/chat/completions" and r["model"] == model)


def _ledger(gw: LoggedClient, **params: Any) -> dict[str, Any]:
    r = gw.request("GET", "/api/ledger", params={"page_size": 500, **params})
    assert r.status_code == 200, r.text
    return r.json()


def test_acceptance_operation(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    port = procs.free_port()
    fake_proc, fake_base = procs.start_fake_upstream(tmp_path)
    serve = None
    gw = None
    obs: dict[str, Any] = {"platform": "macOS(Linux、Windows 未涵蓋)"}
    try:
        serve = procs.start_serve(home, port, log_path=tmp_path / "serve.log")
        addrs = procs.wait_listening(serve)
        gw = LoggedClient(f"http://127.0.0.1:{port}")

        # ---- 建成員與 route(管理 API)
        for body in (_member(SLOT1, "api", "openai", MODEL1, fake_base),
                     _member(SLOT2, "api", "openai", MODEL2, fake_base),
                     _member(LOCAL, "local", "hosted_vllm", "local-model", fake_base)):
            r = gw.request("POST", "/api/deployments", json=body)
            assert r.status_code == 201, r.text
        # target:tag "op"(只有這三個成員帶此 tag)
        r = gw.request("POST", "/api/routes", json={"name": ROUTE, "target": {"tags": {"any": ["op"]}}})
        assert r.status_code == 201, r.text
        r = gw.request("PUT", f"/api/routes/{ROUTE}/order", json={"slots": [
            [{"deployment": SLOT1}], [{"deployment": SLOT2}], [{"deployment": LOCAL}]]})
        assert r.status_code == 200, r.text
        r = gw.request("GET", "/api/deployments")
        assert sorted(d["id"] for d in r.json()["deployments"]) == sorted([SLOT1, SLOT2, LOCAL])

        # ---- slot 1 回 429 → 冷卻
        r = httpx.post(fake_base + "/_fake/mode", json={"model": MODEL1, "mode": "http_429"}, timeout=10)
        assert r.status_code == 200
        r1 = gw.chat(ROUTE)
        assert r1.status_code == 200, r1.text
        assert r1.headers["X-Spillway-Served-By"] == SLOT2
        calls_slot1_after_first = _fake_requests(fake_base, MODEL1)
        assert calls_slot1_after_first == 1
        first_rows = _ledger(gw, deployment=SLOT1)["items"]
        assert first_rows[0]["status"] == "rate_limited"
        cooling = gw.request("GET", f"/api/deployments/{SLOT1}").json()["budget"]["exhausted_until"]
        assert cooling is not None

        r2 = gw.chat(ROUTE)
        assert r2.status_code == 200, r2.text
        obs["cooldown"] = {"served_by": r2.headers["X-Spillway-Served-By"],
                           "slot": r2.headers["X-Spillway-Slot"],
                           "slot1_upstream_calls": _fake_requests(fake_base, MODEL1),
                           "slot1_exhausted_until": cooling}
        assert r2.headers["X-Spillway-Served-By"] == SLOT2
        assert r2.headers["X-Spillway-Slot"] == "2"
        assert _fake_requests(fake_base, MODEL1) == calls_slot1_after_first   # 沒有第二次請求

        # ---- local 直呼
        r3 = gw.chat(f"local/{LOCAL}")
        assert r3.status_code == 200, r3.text
        local_row = _ledger(gw, deployment=LOCAL)["items"][0]
        obs["local_direct"] = {"served_by": r3.headers["X-Spillway-Served-By"],
                               "slot_header": r3.headers["X-Spillway-Slot"],
                               "ledger_slot": local_row["slot"]}
        assert r3.headers["X-Spillway-Served-By"] == LOCAL
        assert r3.headers["X-Spillway-Slot"] == "direct"
        assert local_row["slot"] is None and local_row["route_name"] is None

        # ---- prompt 含金鑰
        r4 = gw.chat(ROUTE, content=f"請用 {KEY} 呼叫")
        assert r4.status_code == 200, r4.text
        row = _ledger(gw, deployment=r4.headers["X-Spillway-Served-By"])["items"][0]
        p = gw.request("GET", f"/api/ledger/{row['id']}/payload")
        assert p.status_code == 200, p.text
        obs["payload"] = {"ledger_id": row["id"], "contains_key": KEY in p.text,
                          "redacted": p.json()["redacted"]}
        assert KEY not in p.text and CRED not in p.text
        assert p.json()["redacted"] is True

        # ---- dry-run 前後帳本列數
        before = _ledger(gw)["total"]
        d = gw.request("POST", f"/api/routes/{ROUTE}/dry-run",
                       json={"messages": [{"role": "user", "content": "hello"}]})
        assert d.status_code == 200, d.text
        after = _ledger(gw)["total"]
        obs["dry_run"] = {"ledger_before": before, "ledger_after": after, "first": d.json()["first"],
                          "excluded": d.json()["excluded"]}
        assert before == after
        assert {x["deployment"]: x["reason"] for x in d.json()["excluded"]}.get(SLOT1) == "cooling"

        # ---- stream: true
        r5 = gw.chat(ROUTE, stream=True)
        obs["stream"] = {"status": r5.status_code, "code": r5.json()["error"]["code"]}
        assert r5.status_code == 400
        assert r5.json() == {"error": {"code": "streaming_unsupported",
                                       "message": r5.json()["error"]["message"]}}
        assert _ledger(gw)["total"] == after

        # ---- 監聽位址
        addrs = procs.listening(serve.pid)
        obs["listening"] = addrs
        assert addrs == [f"127.0.0.1:{port}"], addrs
    finally:
        if gw is not None:
            gw.write_log(HTTP_LOG)
            gw.close()
        if serve is not None:
            procs.stop(serve)
        procs.stop(fake_proc)

    assert serve.poll() is not None and fake_proc.poll() is not None
    log_text = HTTP_LOG.read_text(encoding="utf-8")
    assert KEY not in log_text and CRED not in log_text
    for line in log_text.splitlines():
        assert set(json.loads(line)) == {"method", "path", "status", "elapsed_ms"}
    obs["http_log_lines"] = len(log_text.splitlines())
    record_result("operation", obs)

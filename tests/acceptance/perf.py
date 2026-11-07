"""AC-7 / NFR-1(dry-run)與 NFR-2(dashboard)的量測程序(CHG-20261030-02 T15)。

黑箱(CHG-20261107-03 T3;決策 10 / AM-11):serve 為子行程(`tests.blackbox.harness.Serve`,經
`SPILLWAY_CMD`),以可控時鐘 `START_TIME = START` 啟動(凍結;取代轉換前的 FakeClock);量測一律經 HTTP
(httpx 同步 client、keep-alive,打 127.0.0.1 上的 serve)。轉換前另有的行程內 `router_direct` 指標
(直接呼叫 `Router.dry_run()`)已移除(已知限制 3);門檻不變,HTTP 計時較嚴。results 以 `transport`
記錄傳輸方式。

共同前置:`open_serve(serve_factory)`(開啟測試控制介面、可控時鐘)、`seed.seed_nfr_dataset()`(100 成員、
當月 10,000 列帳本)、量測前送一次 `/v1/chat/completions`(model `nfr`)暖 local 健康快取(決策 20:
`route()` 冷快取時探測一次並快取 10 秒;時鐘凍結,量測期間快取一直有效,dry-run 的 local 成員皆為
`healthy`)。

dry-run 量測(AC-7 與 NFR-1 為同一量測,`dry_run_measurement()` 在同一個 pytest 行程內只跑一次、
結果共用):
- 暖機 `DRY_RUN_WARMUP` 次(不計),再連續 `DRY_RUN_COUNT = 1000` 次 `POST /api/routes/nfr/dry-run`
  (經 HTTP 介面,含 JSON 序列化與 TCP 往返;比「路由決策」本身更嚴),每次以 `time.perf_counter()` 計時。
- 前置檢查:100 個成員都出現在 Plan;excluded 的 reason 分布等於轉換前的基準 `BASELINE_EXCLUDED`
  (兩個 cli 成員在 chat route 被 `mode_chat_excludes_cli` 篩除;artifacts/m2/acceptance-results.json
  轉換前的 `excluded: 2`);local 成員皆為 `healthy`。
- 閘門:HTTP 量測的 nearest-rank P95 < 20 ms(spec 11 NFR-1;AC-7)。

dashboard 量測(NFR-2):暖機 `DASHBOARD_WARMUP` 次後連續 `DASHBOARD_COUNT` 次 `GET /api/dashboard`,
P95 < 200 ms。

慢機器(CI)上的說明:門檻不放寬;量測次數與暖機次數如上固定。未達標時先看
`artifacts/m2/acceptance-results.json` 的分布(p50/p95/max),再依「風險與回滾」回到修改治理。
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from tests.acceptance.results import p95, record_result
from tests.acceptance.seed import seed_nfr_dataset

DRY_RUN_WARMUP = 20
DRY_RUN_COUNT = 1000
DASHBOARD_WARMUP = 10
DASHBOARD_COUNT = 200
DRY_RUN_P95_LIMIT_MS = 20.0
DASHBOARD_P95_LIMIT_MS = 200.0
MESSAGES = [{"role": "user", "content": "請幫我檢查這段程式碼的錯誤處理。"}]
# 可控時鐘的起點(轉換前 FakeClock 的預設值)
START = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
TRANSPORT = "http(子行程 serve,127.0.0.1)"
# 轉換前的基準(chat route `nfr`):excluded 只有兩個 cli 成員,reason 皆為 mode_chat_excludes_cli
BASELINE_EXCLUDED = {"m000": "mode_chat_excludes_cli", "m001": "mode_chat_excludes_cli"}

_cache: dict[str, Any] = {}


def summary(samples: list[float]) -> dict[str, float]:
    s = sorted(samples)
    return {"n": len(s), "p50_ms": round(s[len(s) // 2], 3), "p95_ms": round(p95(s), 3),
            "max_ms": round(s[-1], 3), "mean_ms": round(sum(s) / len(s), 3)}


def open_serve(serve_factory: Callable[..., Any], **opts: Any) -> Any:
    """NFR 量測用的 serve:開啟測試控制介面、可控時鐘從 `START` 開始(凍結)。"""
    return serve_factory(start_time=START, **opts)


def prepare(h: Any, fake_base_url: str) -> Any:
    """種子資料 + 暖 local 健康快取。回傳 NfrDataset。"""
    ds = seed_nfr_dataset(h, h.control.now(), fake_base_url)
    r = h.request("POST", "/v1/chat/completions", json={"model": ds.route, "messages": MESSAGES})
    assert r.status_code == 200, r.text
    return ds


def _measure_dry_run(h: Any, ds: Any) -> dict[str, Any]:
    body = {"messages": MESSAGES}
    url = f"/api/routes/{ds.route}/dry-run"
    plan = None
    with h.http() as c:
        for _ in range(DRY_RUN_WARMUP):
            r = c.post(url, json=body)
            assert r.status_code == 200, r.text
            plan = r.json()
        # 前置檢查:100 個成員都出現在 Plan,local 健康快取已暖,excluded 同轉換前的基準
        entries = [e for s in plan["slots"] for e in s["entries"]]
        seen = {e["deployment"] for e in entries} | {x["deployment"] for x in plan["excluded"]}
        assert seen == set(ds.member_ids), len(seen)
        assert all(e["health"] == "healthy" for e in entries if e["kind"] == "local"), entries
        excluded = {x["deployment"]: x["reason"] for x in plan["excluded"]}
        assert excluded == BASELINE_EXCLUDED, excluded

        http: list[float] = []
        for _ in range(DRY_RUN_COUNT):
            t0 = time.perf_counter()
            r = c.post(url, json=body)
            http.append((time.perf_counter() - t0) * 1000)
            assert r.status_code == 200

    with h.db() as conn:
        n_ledger = conn.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0]
    return {"http": summary(http), "transport": TRANSPORT,
            "candidates": len(entries), "excluded": len(plan["excluded"]), "excluded_reasons": excluded,
            "local_health": sorted({e["health"] for e in entries if e["kind"] == "local"}),
            "ledger_rows": n_ledger, "warmup": DRY_RUN_WARMUP}


def dry_run_measurement(serve_factory: Callable[..., Any], fake_api: Any) -> dict[str, Any]:
    if "dry_run" not in _cache:
        h = open_serve(serve_factory)
        ds = prepare(h, fake_api.base_url)
        _cache["dry_run"] = _measure_dry_run(h, ds)
        h.stop()
        record_result("nfr1_ac07_dry_run", _cache["dry_run"])
    return _cache["dry_run"]


def dashboard_measurement(h: Any) -> dict[str, Any]:
    with h.http() as c:
        for _ in range(DASHBOARD_WARMUP):
            r = c.get("/api/dashboard")
            assert r.status_code == 200, r.text
        samples: list[float] = []
        for _ in range(DASHBOARD_COUNT):
            t0 = time.perf_counter()
            r = c.get("/api/dashboard")
            samples.append((time.perf_counter() - t0) * 1000)
            assert r.status_code == 200
    out = {"http": summary(samples), "transport": TRANSPORT, "members": len(r.json()["deployments"]),
           "warmup": DASHBOARD_WARMUP}
    record_result("nfr2_dashboard", out)
    return out

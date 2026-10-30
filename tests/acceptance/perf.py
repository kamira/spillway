"""AC-7 / NFR-1(dry-run)與 NFR-2(dashboard)的量測程序(CHG-20261030-02 T15)。

共同前置:`open_spillway()`(create_app + lifespan + httpx ASGITransport,FakeClock)、
`seed.seed_nfr_dataset()`(100 成員、當月 10,000 列帳本)、量測前送一次 `/v1/chat/completions`
(model `nfr`)暖 local 健康快取(決策 20:`route()` 冷快取時探測一次並快取 10 秒;FakeClock 不前進,
量測期間快取一直有效,dry-run 的 local 成員皆為 `healthy`)。

dry-run 量測(AC-7 與 NFR-1 為同一量測,`dry_run_measurement()` 在同一個 pytest 行程內只跑一次、
結果共用):
- 暖機 `DRY_RUN_WARMUP` 次(不計),再連續 `DRY_RUN_COUNT = 1000` 次 `POST /api/routes/nfr/dry-run`
  (經 HTTP 介面,含 JSON 序列化;比「路由決策」本身更嚴),每次以 `time.perf_counter()` 計時。
- 另以同樣次數直接呼叫 `app.state.router.dry_run()` 量測路由決策本身(不含 HTTP),一併回報。
- 閘門:HTTP 量測的 nearest-rank P95 < 20 ms(spec 11 NFR-1;AC-7)。

dashboard 量測(NFR-2):暖機 `DASHBOARD_WARMUP` 次後連續 `DASHBOARD_COUNT` 次 `GET /api/dashboard`,
P95 < 200 ms。

慢機器(CI)上的說明:門檻不放寬;量測次數與暖機次數如上固定。未達標時先看
`artifacts/m2/acceptance-results.json` 的分布(p50/p95/max),再依「風險與回滾」回到修改治理。
"""
from __future__ import annotations

import time
from typing import Any

from spillway.domain import ChatRequest
from tests.acceptance.results import p95, record_result
from tests.acceptance.seed import seed_nfr_dataset

DRY_RUN_WARMUP = 20
DRY_RUN_COUNT = 1000
DASHBOARD_WARMUP = 10
DASHBOARD_COUNT = 200
DRY_RUN_P95_LIMIT_MS = 20.0
DASHBOARD_P95_LIMIT_MS = 200.0
MESSAGES = [{"role": "user", "content": "請幫我檢查這段程式碼的錯誤處理。"}]

_cache: dict[str, Any] = {}


def summary(samples: list[float]) -> dict[str, float]:
    s = sorted(samples)
    return {"n": len(s), "p50_ms": round(s[len(s) // 2], 3), "p95_ms": round(p95(s), 3),
            "max_ms": round(s[-1], 3), "mean_ms": round(sum(s) / len(s), 3)}


async def prepare(h: Any, fake_base_url: str) -> Any:
    """種子資料 + 暖 local 健康快取。回傳 NfrDataset。"""
    with h.db() as conn:
        ds = seed_nfr_dataset(conn, h.clock.now(), fake_base_url)
    r = await h.client.post("/v1/chat/completions", json={"model": ds.route, "messages": MESSAGES})
    assert r.status_code == 200, r.text
    return ds


async def _measure_dry_run(h: Any, ds: Any) -> dict[str, Any]:
    body = {"messages": MESSAGES}
    url = f"/api/routes/{ds.route}/dry-run"
    plan = None
    for _ in range(DRY_RUN_WARMUP):
        r = await h.client.post(url, json=body)
        assert r.status_code == 200, r.text
        plan = r.json()
    # 前置檢查:100 個成員都出現在 Plan,local 健康快取已暖
    entries = [e for s in plan["slots"] for e in s["entries"]]
    seen = {e["deployment"] for e in entries} | {x["deployment"] for x in plan["excluded"]}
    assert seen == set(ds.member_ids), len(seen)
    assert all(e["health"] == "healthy" for e in entries if e["kind"] == "local"), entries

    http: list[float] = []
    for _ in range(DRY_RUN_COUNT):
        t0 = time.perf_counter()
        r = await h.client.post(url, json=body)
        http.append((time.perf_counter() - t0) * 1000)
        assert r.status_code == 200

    router = h.app.state.router
    req = ChatRequest(model=f"route:{ds.route}", messages=MESSAGES)
    direct: list[float] = []
    for _ in range(DRY_RUN_COUNT):
        t0 = time.perf_counter()
        await router.dry_run(f"route:{ds.route}", req)
        direct.append((time.perf_counter() - t0) * 1000)

    with h.db() as conn:
        n_ledger = conn.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0]
    return {"http": summary(http), "router_direct": summary(direct),
            "candidates": len(entries), "excluded": len(plan["excluded"]),
            "ledger_rows": n_ledger, "warmup": DRY_RUN_WARMUP}


async def dry_run_measurement(open_spillway: Any, fake_api: Any) -> dict[str, Any]:
    if "dry_run" not in _cache:
        async with open_spillway() as h:
            ds = await prepare(h, fake_api.base_url)
            _cache["dry_run"] = await _measure_dry_run(h, ds)
        record_result("nfr1_ac07_dry_run", _cache["dry_run"])
    return _cache["dry_run"]


async def dashboard_measurement(h: Any) -> dict[str, Any]:
    for _ in range(DASHBOARD_WARMUP):
        r = await h.client.get("/api/dashboard")
        assert r.status_code == 200, r.text
    samples: list[float] = []
    for _ in range(DASHBOARD_COUNT):
        t0 = time.perf_counter()
        r = await h.client.get("/api/dashboard")
        samples.append((time.perf_counter() - t0) * 1000)
        assert r.status_code == 200
    out = {"http": summary(samples), "members": len(r.json()["deployments"]),
           "warmup": DASHBOARD_WARMUP}
    record_result("nfr2_dashboard", out)
    return out

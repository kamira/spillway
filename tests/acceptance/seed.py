"""NFR-1、NFR-2 資料量的種子產生器(CHG-20261030-02 T15;spec 11:100 個成員、當月 10,000 列帳本)。

`seed_nfr_dataset(serve, now, fake_base_url, *, members=100, rows=10_000, seed=7) -> NfrDataset`

黑箱(CHG-20261107-03 T3;附錄 seed.py):`serve` 為 `tests.blackbox.harness.Serve`(須開啟測試控制介面);
成員與 route 經管理 API(`POST /api/deployments`、`POST /api/routes`)建立,帳本列經
`POST /__test__/ledger` 種入(spec 13.4.3;種入不經遮罩,本資料集不含任何金鑰樣字串)。

- 成員 100 個(id `m000`–`m099`,全部帶 tag `nfr`):
  - 68 個 openai api(模型輪流取自價格表內的 `OPENAI_MODELS`,`api_base` 指向假上游 `/v1`);
  - 2 個 cli(CHG-20261101-03 T9):`m000` provider `claude-cli`(`claude-sonnet-5-5`,credit_pool 每月)、
    `m001` provider `codex-cli`(`gpt-5.5`,額度類型 none);`params.max_concurrency` 1;
    route `nfr` 為 chat mode,兩者在候選篩選第 2 條(`mode_chat_excludes_cli`)即被篩除,
    不會走到冷卻與併發名額的檢查;dashboard 仍照常計算它們的額度列;量測期間不起任何子行程;
  - 20 個 anthropic api(`claude-sonnet-4-5`,`api_base` 指向假上游);
  - 10 個 local(provider `hosted_vllm`,決策 10)。
  api 成員憑證一律 `env:SPILLWAY_TEST_KEY`。額度依序輪替:usd 每月(reset_anchor 1–28 輪替)、
  credit_pool 每週、usd 每日、type none、沒有 budgets 列——讓 dashboard 與 QuotaService 走過每一種期間。
- route `nfr`:target `{tags: {any: ["nfr"]}}`、`budget_precheck = 1`(每個雲端成員都要算預估與剩餘,
  最重的路徑),不手動排序(4.3.2 預設層級)。
- 帳本 `rows` 列,`ts` 均勻分布在當月 1 日 00:00 至 `now` 之間(當月),`route_name` 為 `nfr`;
  status 約 85% ok、其餘 error / rate_limited / quota_exhausted / skipped_precheck;成員輪流分配;
  `cost_usd` 很小(每列 ≤ 0.0002 美元),不會讓任何成員額度用完。
- 亂數一律經 `random.Random(seed)`(與轉換前的 `SystemRng(seed)` 同一序列),同一 seed 產生相同資料。
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from tests.blackbox.control import iso

ROUTE = "nfr"
TAG = "nfr"
OPENAI_MODELS = ("gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1", "gpt-4.1-nano")
ANTHROPIC_MODEL = "claude-sonnet-4-5"
CLI_MEMBERS = (("claude-cli", "claude-sonnet-5-5"), ("codex-cli", "gpt-5.5"))   # m000、m001
STATUSES = ("ok",) * 17 + ("error", "rate_limited", "quota_exhausted")
LEDGER_BATCH = 1000     # 每次 `POST /__test__/ledger` 的列數


@dataclass(frozen=True)
class NfrDataset:
    route: str
    member_ids: tuple[str, ...]
    local_ids: tuple[str, ...]
    ledger_rows: int
    cli_ids: tuple[str, ...] = ()


def _budget(i: int) -> dict[str, Any] | None:
    k = i % 5
    if k == 0:
        return {"type": "usd", "limit_amount": 50.0, "window": "monthly", "reset_anchor": 1 + i % 28}
    if k == 1:
        return {"type": "credit_pool", "limit_amount": 20.0, "window": "weekly"}
    if k == 2:
        return {"type": "usd", "limit_amount": 5.0, "window": "daily"}
    if k == 3:
        return {"type": "none", "limit_amount": None, "window": "none"}
    return None


def _members(fake_base_url: str, n: int) -> list[dict[str, Any]]:
    """成員的管理 API body(`POST /api/deployments`)。"""
    n_local = max(1, n // 10)
    n_anthropic = n // 5
    out: list[dict[str, Any]] = []
    for i in range(n):
        dep_id = f"m{i:03d}"
        caps = {"ctx": 128000, "tools": True, "vision": False, "efforts": ["low", "medium", "high"]}
        if i < len(CLI_MEMBERS):
            provider, model = CLI_MEMBERS[i]
            budget = ({"type": "credit_pool", "limit_amount": 20.0, "window": "monthly", "reset_anchor": 1}
                      if provider == "claude-cli" else {"type": "none", "limit_amount": None, "window": "none"})
            out.append({
                "id": dep_id, "model_name": dep_id, "kind": "cli", "provider": provider, "model": model,
                "params": {"max_concurrency": 1}, "credential_ref": None,
                "capabilities": {"ctx": 272000, "tools": False, "vision": False,
                                 "efforts": ["low", "medium", "high"]},
                "tags": [TAG], "budget": budget})
        elif i >= n - n_local:
            out.append({
                "id": dep_id, "model_name": dep_id, "kind": "local", "provider": "hosted_vllm",
                "model": "local-model", "params": {"api_base": f"{fake_base_url}/v1"}, "credential_ref": None,
                "capabilities": {"ctx": 32768, "tools": True, "vision": False, "efforts": []},
                "tags": [TAG]})
        elif i >= n - n_local - n_anthropic:
            out.append({
                "id": dep_id, "model_name": dep_id, "kind": "api", "provider": "anthropic", "model": ANTHROPIC_MODEL,
                "params": {"api_base": fake_base_url}, "credential_ref": "env:SPILLWAY_TEST_KEY",
                "capabilities": caps, "tags": [TAG], "budget": _budget(i)})
        else:
            out.append({
                "id": dep_id, "model_name": dep_id, "kind": "api", "provider": "openai",
                "model": OPENAI_MODELS[i % len(OPENAI_MODELS)],
                "params": {"api_base": f"{fake_base_url}/v1"}, "credential_ref": "env:SPILLWAY_TEST_KEY",
                "capabilities": caps, "tags": [TAG], "budget": _budget(i)})
    return out


def seed_nfr_dataset(serve: Any, now: datetime, fake_base_url: str, *,
                     members: int = 100, rows: int = 10_000, seed: int = 7) -> NfrDataset:
    now = now.astimezone(timezone.utc)
    rng = random.Random(seed)
    deps = _members(fake_base_url, members)
    with serve.http() as c:
        for d in deps:
            r = c.post("/api/deployments", json=d)
            if r.status_code != 201:
                raise RuntimeError(f"建立成員 {d['id']} 失敗:{r.status_code} {r.text}")
        r = c.post("/api/routes", json={"name": ROUTE, "target": {"tags": {"all": [], "any": [TAG], "none": []}},
                                        "mode": "chat", "tags": [], "record_context": None,
                                        "budget_precheck": True})
        if r.status_code != 201:
            raise RuntimeError(f"建立 route 失敗:{r.status_code} {r.text}")
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    span = (now - month_start).total_seconds()
    values: list[dict[str, Any]] = []
    for i in range(rows):
        dep = deps[i % len(deps)]
        ts = month_start + timedelta(seconds=int(rng.random() * span))
        status = STATUSES[int(rng.random() * len(STATUSES))] if dep["kind"] != "local" else "ok"
        if dep["kind"] != "local" and i % 50 == 0:
            status = "skipped_precheck"
        ok = status == "ok"
        tokens_out = 20 + int(rng.random() * 4000) if ok else None
        cost = 0.0 if dep["kind"] == "local" else (round(rng.random() * 0.0002, 8) if ok else None)
        if dep["provider"] == "codex-cli" and ok:
            cost = None   # spec 5.2.3:codex 花費為 NULL
        values.append({
            "ts": iso(ts), "route_name": ROUTE, "deployment_id": dep["id"], "effort": None,
            "tokens_in": 100 if ok else None, "tokens_out": tokens_out, "cost_usd": cost, "status": status,
            "slot": {"api": 2, "cli": 1}.get(dep["kind"], 3), "fallback_depth": 0,
            "latency_ms": 120 if status != "skipped_precheck" else None,
            "est_tokens_in": 100, "est_tokens_out_p50": 500, "est_tokens_out_p90": 2000,
            "est_cost_usd_p90": 0.001 if dep["kind"] != "local" else None,
            "error": None if ok or status == "skipped_precheck" else "upstream error",
        })
    for start in range(0, len(values), LEDGER_BATCH):
        serve.control.ledger(values[start:start + LEDGER_BATCH])
    return NfrDataset(route=ROUTE, member_ids=tuple(d["id"] for d in deps),
                      local_ids=tuple(d["id"] for d in deps if d["kind"] == "local"), ledger_rows=rows,
                      cli_ids=tuple(d["id"] for d in deps if d["kind"] == "cli"))

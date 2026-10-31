"""NFR-1、NFR-2 資料量的種子產生器(CHG-20261030-02 T15;spec 11:100 個成員、當月 10,000 列帳本)。

`seed_nfr_dataset(conn, now, fake_base_url, *, members=100, rows=10_000, seed=7) -> NfrDataset`

- 成員 100 個(id `m000`–`m099`,全部帶 tag `nfr`):
  - 70 個 openai api(模型輪流取自價格表內的 `OPENAI_MODELS`,`api_base` 指向假上游 `/v1`);
  - 20 個 anthropic api(`claude-sonnet-4-5`,`api_base` 指向假上游);
  - 10 個 local(provider `hosted_vllm`,決策 10)。
  api 成員憑證一律 `env:SPILLWAY_TEST_KEY`。額度依序輪替:usd 每月(reset_anchor 1–28 輪替)、
  credit_pool 每週、usd 每日、type none、沒有 budgets 列——讓 dashboard 與 QuotaService 走過每一種期間。
- route `nfr`:target `{tags: {any: ["nfr"]}}`、`budget_precheck = 1`(每個雲端成員都要算預估與剩餘,
  最重的路徑),不手動排序(4.3.2 預設層級)。
- 帳本 `rows` 列,`ts` 均勻分布在當月 1 日 00:00 至 `now` 之間(當月),`route_name` 為 `nfr`;
  status 約 85% ok、其餘 error / rate_limited / quota_exhausted / skipped_precheck;成員輪流分配;
  `cost_usd` 很小(每列 ≤ 0.0002 美元),不會讓任何成員額度用完。
- 亂數一律經 `spillway.clock.SystemRng(seed)`,同一 seed 產生相同資料。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from spillway.clock import SystemRng, to_iso
from spillway.domain import Budget, Deployment
from spillway.store import repo
from spillway.store.db import transaction

ROUTE = "nfr"
TAG = "nfr"
OPENAI_MODELS = ("gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1", "gpt-4.1-nano")
ANTHROPIC_MODEL = "claude-sonnet-4-5"
STATUSES = ("ok",) * 17 + ("error", "rate_limited", "quota_exhausted")


@dataclass(frozen=True)
class NfrDataset:
    route: str
    member_ids: tuple[str, ...]
    local_ids: tuple[str, ...]
    ledger_rows: int


def _budget(i: int) -> Budget | None:
    k = i % 5
    if k == 0:
        return Budget(type="usd", limit_amount=50.0, window="monthly", reset_anchor=1 + i % 28)
    if k == 1:
        return Budget(type="credit_pool", limit_amount=20.0, window="weekly")
    if k == 2:
        return Budget(type="usd", limit_amount=5.0, window="daily")
    if k == 3:
        return Budget(type="none", limit_amount=None, window="none")
    return None


def _members(fake_base_url: str, n: int) -> list[Deployment]:
    n_local = max(1, n // 10)
    n_anthropic = n // 5
    out: list[Deployment] = []
    for i in range(n):
        dep_id = f"m{i:03d}"
        caps = {"ctx": 128000, "tools": True, "vision": False, "efforts": ["low", "medium", "high"]}
        if i >= n - n_local:
            out.append(Deployment(
                id=dep_id, model_name=dep_id, kind="local", provider="hosted_vllm",
                model="local-model", params={"api_base": f"{fake_base_url}/v1"}, credential_ref=None,
                capabilities={"ctx": 32768, "tools": True, "vision": False, "efforts": []},
                tags=(TAG,)))
        elif i >= n - n_local - n_anthropic:
            out.append(Deployment(
                id=dep_id, model_name=dep_id, kind="api", provider="anthropic", model=ANTHROPIC_MODEL,
                params={"api_base": fake_base_url}, credential_ref="env:SPILLWAY_TEST_KEY",
                capabilities=caps, tags=(TAG,), budget=_budget(i)))
        else:
            out.append(Deployment(
                id=dep_id, model_name=dep_id, kind="api", provider="openai",
                model=OPENAI_MODELS[i % len(OPENAI_MODELS)],
                params={"api_base": f"{fake_base_url}/v1"}, credential_ref="env:SPILLWAY_TEST_KEY",
                capabilities=caps, tags=(TAG,), budget=_budget(i)))
    return out


def seed_nfr_dataset(conn: sqlite3.Connection, now: datetime, fake_base_url: str, *,
                     members: int = 100, rows: int = 10_000, seed: int = 7) -> NfrDataset:
    now = now.astimezone(timezone.utc)
    rng = SystemRng(seed=seed)
    deps = _members(fake_base_url, members)
    for d in deps:
        repo.save_deployment(conn, d)
    repo.save_route(conn, repo.Route(name=ROUTE, target={"tags": {"all": [], "any": [TAG], "none": []}},
                                     mode="chat", tags=(), record_context=None, budget_precheck=True))
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    span = (now - month_start).total_seconds()
    cloud = [d for d in deps if d.kind != "local"]
    values = []
    for i in range(rows):
        dep = deps[i % len(deps)]
        ts = month_start + timedelta(seconds=int(rng.random() * span))
        status = STATUSES[int(rng.random() * len(STATUSES))] if dep.kind != "local" else "ok"
        if dep.kind != "local" and i % 50 == 0:
            status = "skipped_precheck"
        ok = status == "ok"
        tokens_out = 20 + int(rng.random() * 4000) if ok else None
        cost = 0.0 if dep.kind == "local" else (round(rng.random() * 0.0002, 8) if ok else None)
        values.append((
            to_iso(ts), ROUTE, dep.id, None, 100 if ok else None, tokens_out, cost, status,
            2 if dep.kind == "api" else 3, 0, 120 if status != "skipped_precheck" else None,
            100, 500, 2000, 0.001 if dep.kind != "local" else None,
            None if ok or status == "skipped_precheck" else "upstream error",
        ))
    with transaction(conn):
        conn.executemany(
            "INSERT INTO usage_ledger (ts, route_name, deployment_id, effort, tokens_in, tokens_out, "
            "cost_usd, status, slot, fallback_depth, latency_ms, est_tokens_in, est_tokens_out_p50, "
            "est_tokens_out_p90, est_cost_usd_p90, error) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
    return NfrDataset(route=ROUTE, member_ids=tuple(d.id for d in deps),
                      local_ids=tuple(d.id for d in deps if d.kind == "local"), ledger_rows=rows)

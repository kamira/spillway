"""`/api/dashboard`(CHG-20261101-02 決策 9)。"""
from __future__ import annotations

from datetime import timezone
from typing import Any

from fastapi import Request

from spillway.api.deps import request_conn
from spillway.clock import to_iso
from spillway.quota import member_quota_rows

from .common import _clock, router


# ================================================================ /api/dashboard
# 推導本體在 `spillway.quota.member_quota_rows`(CHG-20261102-01 決策 12;MCP `quota_status` 共用)


@router.get("/dashboard")
async def dashboard(request: Request) -> dict[str, Any]:
    """各成員額度總覽(spec 9.2、5.1、5.4.6;決策 6)。

    回應 `{generated_at, deployments: [...]}`,每個成員(依 id,只列現存成員;孤兒帳本列不計):
    `id`、`model_name`、`kind`、`provider`、`enabled`、`budget_type`、`limit_amount`、`window`、
    `period_start`、`next_reset`、`period_spend`、`remaining`、`unlimited`、`ratio`、`cooling`、
    `cooling_until`、`cooling_reason`、`requests_24h`、`est_days_left`。

    - 本期花費 = 帳本該成員 `ts` ∈ [period_start, next_reset) 的 `cost_usd` 加總(不分 status,
      NULL 不計),與 `QuotaService.remaining()` 相同;window `none` 為累計(period_start、next_reset
      為 null)。沒有 budgets 列的成員以日曆月(1 日起)計本期花費,`window` 為 null。
      舊版留下的暫行列(`type=none`、`window=none`、`limit_amount` NULL;U10 (a)、決策 59;schema v2
      migration 保留這些列)比照沒有 budgets 列:日曆月計、`budget_type` 與 `window` 為 null。
    - 無上限(無 budgets 列、type `none`、`limit_amount` NULL):`remaining` null、`unlimited` true、
      `ratio` 1。否則 `remaining = limit − 本期花費`(可為負)、`ratio` 夾在 0–1、上限 0 時 0。
    - `cooling_until`、`cooling_reason`:`cooldowns` 列尚未到期時的期限與原因,否則 null;`cooling` 為其布林
      (CHG-20261101-03 決策 6)。
    - `requests_24h`:近 24 小時(`ts ≥ now − 24h`)帳本列數,不含 `skipped_precheck`(沒有送出請求)。
    - `est_days_left`(5.4.6):`max(remaining, 0) ÷ (近 7 天實際花費 ÷ 7)`;無上限或近 7 天無花費時 null。

    效能(NFR-2):查詢數與成員數無關——成員、budgets 與 cooldowns 一次 JOIN、每種相異期間一次 GROUP BY 加總、
    近 24 小時請求數與近 7 天花費合併一次 GROUP BY(`repo.dashboard_members`、`repo.spend_by_deployment`、
    `repo.recent_activity`;不用 `QuotaService` 的逐成員加總)。
    """
    clock = _clock(request)
    now = clock.now().astimezone(timezone.utc)
    with request_conn(request) as conn:
        rows = member_quota_rows(conn, clock, now)
    return {"generated_at": to_iso(now), "deployments": rows}

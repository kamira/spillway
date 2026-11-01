"""排除原因值快照(CHG-20261101-03 T6;契約清單「排除原因 `concurrency_full`、`shutting_down`」)。

`EXCLUDE_REASONS` 出現在 dry-run、503 body 與 M4 UI-4;增減或改順序必須經修改治理。
"""
from __future__ import annotations

import spillway.domain as domain
import spillway.routing as routing
from spillway.routing import filter as routing_filter

# 刻意寫死(值與順序);`concurrency_full` 緊接 `cooling`(決策 7),`shutting_down` 由 Router 產生(決策 16)
EXCLUDE_REASONS_SNAPSHOT = (
    "disabled",
    "mode_chat_excludes_cli",
    "ctx_exceeded",
    "tools_unsupported",
    "vision_unsupported",
    "adapter_unavailable",
    "cooling",
    "concurrency_full",
    "quota_exhausted",
    "unhealthy",
    "skipped_precheck",
    "shutting_down",
)


def test_exclude_reasons_snapshot():
    assert routing_filter.EXCLUDE_REASONS == EXCLUDE_REASONS_SNAPSHOT
    assert routing.EXCLUDE_REASONS is routing_filter.EXCLUDE_REASONS


def test_new_reason_constants_live_in_domain():
    assert domain.EXCLUDE_CONCURRENCY_FULL == "concurrency_full"
    assert domain.EXCLUDE_SHUTTING_DOWN == "shutting_down"


def test_router_only_reasons_are_not_ledger_statuses():
    # 兩者都不寫帳本(決策 7、16)
    assert "concurrency_full" not in domain.LEDGER_STATUSES
    assert "shutting_down" not in domain.LEDGER_STATUSES

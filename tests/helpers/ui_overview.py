"""UI-1 總覽的種資料 helper(CHG-20261103-01 T4;決策 22)。

成員與 route 經 `tests.helpers.ui` 的 `add_member`、`add_route` 建立;冷卻沒有 `/api` 寫入端點,
經 `tests/helpers/ui.db_execute` 寫入。

- `set_cooldown(gw, dep_id, until, reason)`。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from spillway.clock import to_iso
from tests.helpers.ui import db_execute

__all__ = ["set_cooldown"]


def set_cooldown(gw: Any, dep_id: str, until: datetime, reason: str) -> None:
    db_execute(gw, "INSERT INTO cooldowns (deployment_id, exhausted_until, reason) VALUES (?, ?, ?) "
                   "ON CONFLICT(deployment_id) DO UPDATE SET exhausted_until = excluded.exhausted_until, "
                   "reason = excluded.reason",
               [dep_id, to_iso(until), reason])

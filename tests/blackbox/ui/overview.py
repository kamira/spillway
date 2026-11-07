"""UI-1 總覽的種資料 helper(CHG-20261103-01 T4;決策 22)。

(CHG-20261107-03 T2 由 `tests/helpers/ui_overview.py` 複製改寫成黑箱版;原檔不動,T5 改用本模組後刪除)
成員與 route 經 `tests.blackbox.ui` 的 `add_member`、`add_route` 建立;冷卻沒有 `/api` 寫入端點,
經測試控制介面 `POST /__test__/cooldowns`(spec 13.4.4;覆寫)寫入。

- `set_cooldown(gw, dep_id, until, reason)`。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

__all__ = ["set_cooldown"]


def set_cooldown(gw: Any, dep_id: str, until: datetime, reason: str) -> None:
    gw.control.cooldown(dep_id, until, reason)

"""Effort 正規化、來源優先序與降級(spec 6.1、6.2;決策 12、29)。

- `normalize_effort(value)` / `InvalidEffort`:請求 `reasoning_effort` 正規化(CHG-20261030-02 決策 29;
  CHG-20261101-02 決策 8 由 `routing.router` 移入,`spillway.routing` 與 `spillway.routing.router` 轉匯出)。
- `EffortMapper.pick`:挑選與降級,回傳內部等級字串;內部等級轉上游值由
  `spillway.adapters.base.effort_to_upstream` 負責。
"""
from __future__ import annotations

from collections.abc import Sequence

from spillway.domain import EFFORTS

__all__ = ["EffortMapper", "InvalidEffort", "normalize_effort"]

_EFFORT_ALIASES = {"minimal": "none"}


class InvalidEffort(ValueError):
    """請求的 `reasoning_effort` 不在五級內(決策 29;T13 轉 422 `validation_error`)。

    訊息不回顯原值以外的內容;原值放在 `value`。
    """

    def __init__(self, value: object) -> None:
        super().__init__(
            f"reasoning_effort 不合法:{value!r},只可為 {', '.join(EFFORTS)}(或 minimal)"
        )
        self.value = value


def normalize_effort(value: object) -> str | None:
    """決策 29:None 或空白字串 → None;不分大小寫;`minimal` → `none`;其餘不在五級內丟 `InvalidEffort`。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidEffort(value)
    v = value.strip().lower()
    if not v:
        return None
    v = _EFFORT_ALIASES.get(v, v)
    if v not in EFFORTS:
        raise InvalidEffort(value)
    return v


def _check(level: str, what: str) -> None:
    if level not in EFFORTS:
        raise ValueError(f"{what} 不是合法 effort 等級 {level!r},只可為 {', '.join(EFFORTS)}")


class EffortMapper:
    @staticmethod
    def pick(
        route_target_effort: str | None,
        req_effort: str | None,
        caps_efforts: Sequence[str] | None,
    ) -> str | None:
        """依 6.1 取第一個非空值(route_target 優先,空字串視為空),再依 6.2 降級。

        降級:所需等級不在 `caps_efforts` 時,取 `EFFORTS` 順序(none 最低、max 最高)中
        比所需等級低、且在清單內的最近一級;沒有較低等級回 None(不傳)。

        `caps_efforts` 為 None(成員未宣告能力)視為不限制,回傳原值,
        理由:沒有資料就不應擅自改變使用者要求。
        `caps_efforts` 為空序列(明確宣告不支援任何等級)回 None,
        理由:依 6.2.1 無任何支援等級即「不傳」。

        任一輸入的等級字串不合法(含 caps 內的項目)時丟 ValueError。
        """
        for name, value in (("route_target_effort", route_target_effort), ("req_effort", req_effort)):
            if value:
                _check(value, name)
        if caps_efforts is not None:
            for c in caps_efforts:
                _check(c, "caps_efforts 項目")

        wanted = route_target_effort or req_effort or None
        if wanted is None or caps_efforts is None:
            return wanted
        supported = set(caps_efforts)
        for level in reversed(EFFORTS[: EFFORTS.index(wanted) + 1]):
            if level in supported:
                return level
        return None

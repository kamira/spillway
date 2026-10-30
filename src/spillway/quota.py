"""額度計算與冷卻(CHG-20261030-02 T7;spec 5、5.1、5.3.2、5.3.3、4.2.4、4.2.6、4.5.2;design D19)。

上游訊號歸類不在這裡(決策 12:`adapters.base.classify`);本模組只依歸類後的 status 設冷卻。
"""
from __future__ import annotations

import calendar
import math
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from spillway.clock import Clock, to_iso
from spillway.domain import Budget, Deployment
from spillway.store import repo

RATE_LIMIT_COOLDOWN = timedelta(seconds=60)   # spec 4.5.2
NONE_WINDOW_REPROBE = timedelta(hours=1)      # spec 5.1 window=none

ConnSource = sqlite3.Connection | Callable[[], sqlite3.Connection]


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("需要帶時區的時間")
    return dt.astimezone(timezone.utc)


def _anchored(year: int, month: int, anchor: int) -> datetime:
    # reset_anchor 依 schema 為 1–28;超出時夾到該月最後一天,避免不存在的日期
    day = min(max(anchor, 1), calendar.monthrange(year, month)[1])
    return datetime(year, month, day, tzinfo=timezone.utc)


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


class QuotaService:
    """成員的期間、剩餘額度、比例與冷卻。

    建構:`QuotaService(conn, clock)`。`conn` 可以是 `store.db.connect()` 開出的
    `sqlite3.Connection`,或回傳此類連線的零參數工廠(每次操作呼叫一次,例如回傳
    thread-local 連線)。QuotaService 不開也不關連線。

    慣例:
    - 各方法的 `now` 可省略,省略時取 `clock.now()`;一律需帶時區,內部換成 UTC。
    - 預算狀態以資料庫 budgets 列為準(`repo.get_budget(conn, dep.id)`),不用 `dep.budget`
      快照——冷卻可能由其他請求剛設定。
    - monthly 的 `reset_anchor` 為 NULL 時以 1 計;超過該月天數時取該月最後一天。
    - window `none` 的 `period_start` 回 None(累計,不設起點)。
    - 無上限(決策 6 / D19):無 budgets 列、`type = none`、或 `limit_amount` 為 NULL 時
      `remaining` 回 `math.inf`、`ratio` 回 1。上限 0 時剩餘 ≤ 0、比例 0;比例夾在 0–1。
    - 本期花費 = 帳本該成員 `ts` 落在 [period_start, next_reset) 的 `cost_usd` 加總,
      不分 status,NULL 不計。
    """

    def __init__(self, conn: ConnSource, clock: Clock) -> None:
        self._conn_src = conn
        self._clock = clock

    # ------------------------------------------------------------ 共用

    def _conn(self) -> sqlite3.Connection:
        src = self._conn_src
        return src if isinstance(src, sqlite3.Connection) else src()

    def _now(self, now: datetime | None) -> datetime:
        return _utc(self._clock.now() if now is None else now)

    def _budget(self, dep: Deployment) -> Budget | None:
        return repo.get_budget(self._conn(), dep.id)

    # ------------------------------------------------------------ 期間(5.1)

    def period_start(self, budget: Budget, now: datetime | None = None) -> datetime | None:
        now = self._now(now)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if budget.window == "daily":
            return midnight
        if budget.window == "weekly":
            return midnight - timedelta(days=midnight.weekday())
        if budget.window == "monthly":
            anchor = budget.reset_anchor or 1
            start = _anchored(now.year, now.month, anchor)
            if start > now:
                y, m = _shift_month(now.year, now.month, -1)
                start = _anchored(y, m, anchor)
            return start
        if budget.window == "none":
            return None
        raise ValueError(f"未知的 window: {budget.window!r}")

    def next_reset(self, budget: Budget, now: datetime | None = None) -> datetime:
        now = self._now(now)
        if budget.window == "none":
            return now + NONE_WINDOW_REPROBE
        start = self.period_start(budget, now)
        assert start is not None
        if budget.window == "daily":
            return start + timedelta(days=1)
        if budget.window == "weekly":
            return start + timedelta(days=7)
        y, m = _shift_month(start.year, start.month, 1)
        return _anchored(y, m, budget.reset_anchor or 1)

    # ------------------------------------------------------------ 額度(5、決策 6)

    @staticmethod
    def _unlimited(budget: Budget | None) -> bool:
        return budget is None or budget.type == "none" or budget.limit_amount is None

    def _spent(self, dep_id: str, budget: Budget, now: datetime) -> float:
        # repo 沒有加總函式;直接以參數化 SQL 查 usage_ledger。ts 為固定格式 UTC 字串,可字典序比較。
        sql = "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_ledger WHERE deployment_id = ?"
        args: list[object] = [dep_id]
        start = self.period_start(budget, now)
        if start is not None:
            sql += " AND ts >= ? AND ts < ?"
            args += [to_iso(start), to_iso(self.next_reset(budget, now))]
        return float(self._conn().execute(sql, args).fetchone()[0])

    def remaining(self, dep: Deployment, now: datetime | None = None) -> float:
        now = self._now(now)
        budget = self._budget(dep)
        if self._unlimited(budget):
            return math.inf
        assert budget is not None and budget.limit_amount is not None
        return float(budget.limit_amount) - self._spent(dep.id, budget, now)

    def ratio(self, dep: Deployment, now: datetime | None = None) -> float:
        now = self._now(now)
        budget = self._budget(dep)
        if self._unlimited(budget):
            return 1.0
        assert budget is not None and budget.limit_amount is not None
        limit = float(budget.limit_amount)
        if limit <= 0:
            return 0.0
        left = limit - self._spent(dep.id, budget, now)
        return min(1.0, max(0.0, left / limit))

    # ------------------------------------------------------------ 冷卻(4.5.2、5.3.2、5.3.3、4.2.6)

    def is_cooling(self, dep: Deployment, now: datetime | None = None) -> bool:
        """`exhausted_until` 尚未到期為 True;到期即自動恢復(4.2.6)。"""
        now = self._now(now)
        budget = self._budget(dep)
        return bool(budget and budget.exhausted_until and budget.exhausted_until > now)

    def mark(self, dep: Deployment, status: str, now: datetime | None = None) -> datetime | None:
        """依 4.5.2 設冷卻並回傳新的 `exhausted_until`。

        `quota_exhausted` 設為下一重置點(5.3.2;window none 為現在 + 1 小時),
        `rate_limited` 設為現在 + 60 秒;其他 status 不動,回傳目前值。
        成員沒有 budgets 列時,冷卻仍須生效(4.5.2):建立 `type=none`、`window=none` 的列
        只為記錄冷卻;依決策 6 這等同「無上限」,不改變額度判斷(施工審議代決)。
        """
        now = self._now(now)
        budget = self._budget(dep)
        if budget is None:
            if status not in ("quota_exhausted", "rate_limited"):
                return None
            budget = Budget(type="none", limit_amount=None, window="none")
            repo.save_budget(self._conn(), dep.id, budget)
        if status == "quota_exhausted":
            until = self.next_reset(budget, now)
        elif status == "rate_limited":
            until = now + RATE_LIMIT_COOLDOWN
        else:
            return budget.exhausted_until
        until = until.replace(microsecond=0)  # 存到秒(spec 2.1.2),回傳值與讀回一致
        repo.set_exhausted_until(self._conn(), dep.id, until)
        return until

    def clear(self, dep: Deployment) -> None:
        """手動清除冷卻(5.3.3)。"""
        repo.set_exhausted_until(self._conn(), dep.id, None)

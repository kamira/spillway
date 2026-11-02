"""額度計算與冷卻(CHG-20261030-02 T7;spec 5、5.1、5.3.2、5.3.3、4.2.4、4.2.6、4.5.2;design D19)。

上游訊號歸類不在這裡(決策 12:`adapters.base.classify`);本模組只依歸類後的 status 設冷卻。
"""
from __future__ import annotations

import calendar
import math
from datetime import datetime, timedelta, timezone

from spillway.clock import Clock
from spillway.domain import COOLING_STATUSES, Budget, Deployment
from spillway.store import repo
from spillway.store.db import ConnSource, use_conn

RATE_LIMIT_COOLDOWN = timedelta(seconds=60)   # spec 4.5.2
NONE_WINDOW_REPROBE = timedelta(hours=1)      # spec 5.1 window=none


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
    - 額度設定以資料庫 budgets 列為準(`repo.get_budget(conn, dep.id)`)、冷卻以 cooldowns 列為準
      (`repo.get_cooldown`),不用 `dep.budget` 快照——兩者都可能由其他請求剛改過。
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

    def _now(self, now: datetime | None) -> datetime:
        return _utc(self._clock.now() if now is None else now)

    def _budget(self, dep: Deployment) -> Budget | None:
        with use_conn(self._conn_src) as conn:
            return repo.get_budget(conn, dep.id)

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
        start = self.period_start(budget, now)
        end = None if start is None else self.next_reset(budget, now)
        with use_conn(self._conn_src) as conn:
            return repo.ledger_spend(conn, dep_id, start, end)

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

    # 冷卻存在 `cooldowns` 表(schema v2;CHG-20261101-03 決策 3、6),與額度設定(budgets)分開。

    def _cooldown(self, dep: Deployment) -> repo.Cooldown | None:
        with use_conn(self._conn_src) as conn:
            return repo.get_cooldown(conn, dep.id)

    def is_cooling(self, dep: Deployment, now: datetime | None = None) -> bool:
        """冷卻期限尚未到期為 True;到期即自動恢復(4.2.6)。"""
        now = self._now(now)
        cd = self._cooldown(dep)
        return bool(cd and cd.until > now)

    def mark(self, dep: Deployment, status: str, now: datetime | None = None) -> datetime | None:
        """依 4.5.2 設冷卻並回傳寫入後的冷卻期限(冷卻只延長不縮短)。

        `quota_exhausted` 設為下一重置點(5.3.2;沒有 budgets 列時以 window none 計,為現在 + 1 小時),
        `rate_limited` 設為現在 + 60 秒;其他 status 不動,回傳目前值(沒有冷卻列時 None)。
        冷卻寫入 `cooldowns`(原因即 status);不建立、不修改 budgets 列(決策 6)。
        """
        now = self._now(now)
        if status not in COOLING_STATUSES:
            cd = self._cooldown(dep)
            return cd.until if cd else None
        budget = self._budget(dep) or Budget(type="none", limit_amount=None, window="none")
        if status == "quota_exhausted":
            until = self.next_reset(budget, now)
        else:
            until = now + RATE_LIMIT_COOLDOWN
        until = until.replace(microsecond=0)  # 存到秒(spec 2.1.2)
        with use_conn(self._conn_src) as conn:
            repo.mark_cooldown(conn, dep.id, until, status)
            # 冷卻只延長不縮短(M27):回傳寫入後讀回的實際值
            stored = repo.get_cooldown(conn, dep.id)
        return stored.until if stored else until

    def clear(self, dep: Deployment) -> None:
        """手動清除冷卻(5.3.3):刪除 cooldowns 列。"""
        with use_conn(self._conn_src) as conn:
            repo.clear_cooldown(conn, dep.id)

"""各表存取(CHG-20261030-02 T2;spec 2.3、2.4;決策 24)。

慣例:
- 每個函式的第一個參數是 `store.db.connect()` 開出的連線;函式不開、不關連線。
- 寫入函式自行包在 `db.transaction(conn)` 內:單獨呼叫即原子提交;呼叫端若已在
  `transaction(conn)` 之內,則成為 SAVEPOINT,隨外層一起提交或回滾。
- JSON 欄位(`params`、`capabilities`、`selector`、`target`、`routes.tags`)以
  `json.dumps(..., ensure_ascii=False, sort_keys=True)` 存、`json.loads` 讀。
- 時間欄位以 `clock.to_iso()` 存(UTC、精度到秒,spec 2.1.2),以 `clock.from_iso()` 讀回帶時區的 datetime。
- 布林欄位以 0/1 存;`routes.record_context` 的 NULL 讀回 None(沿用全域,spec 2.4.2)。
- 更新成員與 route 用 UPSERT(`ON CONFLICT DO UPDATE`),不用 `INSERT OR REPLACE`——
  後者會先刪列,觸發 `ON DELETE CASCADE` 把 route_targets 一起刪掉。
- settings 讀取缺鍵時回傳 `SETTING_DEFAULTS` 的常數,不寫入資料列(spec 2、決策 24)。

本模組不做業務驗證(cardinality、credential_ref 格式、群組參照等由應用層負責);
CHECK、外鍵違反直接以 `sqlite3.IntegrityError` 丟出。
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from spillway.domain import STATUS_SKIPPED_PRECHECK, Budget, Deployment
from spillway.clock import from_iso, to_iso
from spillway.store.db import transaction

# ---------------------------------------------------------------- 共用轉換


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _bool_or_none(v: int | None) -> bool | None:
    return None if v is None else bool(v)


def _bool_to_int(v: bool | None) -> int | None:
    return None if v is None else int(v)


# ---------------------------------------------------------------- deployments / tags / budgets


def save_deployment(conn: sqlite3.Connection, dep: Deployment) -> None:
    """新增或更新成員;同一交易內以 `dep.tags` 取代 tags、以 `dep.budget` 取代 budget
    (`budget=None` 會刪除 budgets 列)。既有的 route_targets 不受影響。"""
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO deployments
              (id, model_name, kind, provider, model, params, credential_ref, capabilities, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              model_name = excluded.model_name,
              kind = excluded.kind,
              provider = excluded.provider,
              model = excluded.model,
              params = excluded.params,
              credential_ref = excluded.credential_ref,
              capabilities = excluded.capabilities,
              enabled = excluded.enabled
            """,
            (
                dep.id,
                dep.model_name,
                dep.kind,
                dep.provider,
                dep.model,
                _dumps(dep.params),
                dep.credential_ref,
                _dumps(dep.capabilities),
                int(bool(dep.enabled)),
            ),
        )
        set_tags(conn, dep.id, dep.tags)
        save_budget(conn, dep.id, dep.budget)


def _deployment_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> Deployment:
    return Deployment(
        id=row["id"],
        model_name=row["model_name"],
        kind=row["kind"],
        provider=row["provider"],
        model=row["model"],
        params=json.loads(row["params"]),
        credential_ref=row["credential_ref"],
        capabilities=json.loads(row["capabilities"]),
        enabled=bool(row["enabled"]),
        tags=get_tags(conn, row["id"]),
        budget=get_budget(conn, row["id"]),
    )


def load_deployment(conn: sqlite3.Connection, dep_id: str) -> Deployment | None:
    """把 deployments 列與其 tags(依字母排序的 tuple)、budget 轉成 T4 的 `Deployment`;
    不存在回 None。"""
    row = conn.execute("SELECT * FROM deployments WHERE id = ?", (dep_id,)).fetchone()
    return None if row is None else _deployment_from_row(conn, row)


def list_deployments(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[Deployment]:
    """依 id 排序。"""
    sql = "SELECT * FROM deployments"
    if enabled_only:
        sql += " WHERE enabled = 1"
    rows = conn.execute(sql + " ORDER BY id").fetchall()
    return [_deployment_from_row(conn, r) for r in rows]


def deployment_exists(conn: sqlite3.Connection, dep_id: str) -> bool:
    """成員是否存在。"""
    return conn.execute("SELECT 1 FROM deployments WHERE id = ?", (dep_id,)).fetchone() is not None


def provider_member_ids(conn: sqlite3.Connection, provider: str, *, exclude_id: str) -> list[str]:
    """同 provider 的成員 id(排除 `exclude_id`,依 id 排序);管理 API 的 cardinality 檢查
    (spec 7.6.5)用,呼叫端負責交易。"""
    rows = conn.execute(
        "SELECT id FROM deployments WHERE provider = ? AND id != ? ORDER BY id",
        (provider, exclude_id),
    ).fetchall()
    return [r[0] for r in rows]


def list_kinds_providers(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """每個成員的 `(kind, provider)`(順序不保證);`GET /api/adapters` 計算模組成員數用。"""
    return [(r[0], r[1]) for r in conn.execute("SELECT kind, provider FROM deployments")]


def delete_deployment(conn: sqlite3.Connection, dep_id: str) -> bool:
    """刪除成員;tags、budget、route_targets 由外鍵串聯刪除,帳本保留。回傳是否有刪到。"""
    with transaction(conn):
        cur = conn.execute("DELETE FROM deployments WHERE id = ?", (dep_id,))
    return cur.rowcount > 0


def get_tags(conn: sqlite3.Connection, dep_id: str) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT tag FROM deployment_tags WHERE deployment_id = ? ORDER BY tag", (dep_id,)
    ).fetchall()
    return tuple(r[0] for r in rows)


def set_tags(conn: sqlite3.Connection, dep_id: str, tags: Iterable[str]) -> None:
    """以給定集合取代成員的 tags(重複值合併)。"""
    with transaction(conn):
        conn.execute("DELETE FROM deployment_tags WHERE deployment_id = ?", (dep_id,))
        conn.executemany(
            "INSERT INTO deployment_tags (deployment_id, tag) VALUES (?, ?)",
            [(dep_id, t) for t in sorted(set(tags))],
        )


def get_budget(conn: sqlite3.Connection, dep_id: str) -> Budget | None:
    row = conn.execute("SELECT * FROM budgets WHERE deployment_id = ?", (dep_id,)).fetchone()
    if row is None:
        return None
    return Budget(
        type=row["type"],
        limit_amount=row["limit_amount"],
        window=row["window"],
        reset_anchor=row["reset_anchor"],
    )


def save_budget(conn: sqlite3.Connection, dep_id: str, budget: Budget | None) -> None:
    """新增或取代成員的 budgets 列;`None` 刪除該列(冷卻在 `cooldowns`,不受影響)。"""
    with transaction(conn):
        if budget is None:
            conn.execute("DELETE FROM budgets WHERE deployment_id = ?", (dep_id,))
            return
        conn.execute(
            """
            INSERT INTO budgets (deployment_id, type, limit_amount, window, reset_anchor)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(deployment_id) DO UPDATE SET
              type = excluded.type,
              limit_amount = excluded.limit_amount,
              window = excluded.window,
              reset_anchor = excluded.reset_anchor
            """,
            (dep_id, budget.type, budget.limit_amount, budget.window, budget.reset_anchor),
        )


# ---------------------------------------------------------------- cooldowns(schema v2;CHG-20261101-03 決策 3、6)
# exhausted_until 固定為 `%Y-%m-%dT%H:%M:%SZ`(clock.to_iso),字串比較即時間比較。


@dataclass(frozen=True)
class Cooldown:
    until: datetime
    reason: str          # quota_exhausted | rate_limited


def _cooldown_from_row(row: sqlite3.Row) -> Cooldown:
    return Cooldown(until=from_iso(row["exhausted_until"]), reason=row["reason"])


def get_cooldown(conn: sqlite3.Connection, dep_id: str) -> Cooldown | None:
    """成員的冷卻列(不論是否已到期);沒有時 None。"""
    row = conn.execute(
        "SELECT exhausted_until, reason FROM cooldowns WHERE deployment_id = ?", (dep_id,)
    ).fetchone()
    return None if row is None else _cooldown_from_row(row)


def list_cooldowns(conn: sqlite3.Connection) -> dict[str, Cooldown]:
    """所有冷卻列(不論是否已到期),單一查詢;`{deployment_id: Cooldown}`。"""
    return {
        r["deployment_id"]: _cooldown_from_row(r)
        for r in conn.execute("SELECT deployment_id, exhausted_until, reason FROM cooldowns")
    }


def mark_cooldown(conn: sqlite3.Connection, dep_id: str, until: datetime, reason: str) -> None:
    """單一原子敘述設定冷卻;冷卻只延長、不縮短(末端審查 M27):既有期限較晚(或相同)時整列保留,
    否則期限與原因一起改為新值。不建立、不修改 budgets 列(決策 6,取代決策 59 的暫行列)。
    成員不存在時外鍵違反(`sqlite3.IntegrityError`)。"""
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO cooldowns (deployment_id, exhausted_until, reason) VALUES (?, ?, ?)
            ON CONFLICT(deployment_id) DO UPDATE SET
              exhausted_until = excluded.exhausted_until,
              reason = excluded.reason
            WHERE excluded.exhausted_until > cooldowns.exhausted_until
            """,
            (dep_id, to_iso(until), reason),
        )


def clear_cooldown(conn: sqlite3.Connection, dep_id: str) -> bool:
    """刪除成員的冷卻列(spec 5.3.3);回傳是否有刪到。"""
    with transaction(conn):
        cur = conn.execute("DELETE FROM cooldowns WHERE deployment_id = ?", (dep_id,))
    return cur.rowcount > 0


# ---------------------------------------------------------------- groups


@dataclass(frozen=True)
class Group:
    name: str
    selector: dict[str, Any]  # {all: [tag], any: [tag], none: [tag]}


def save_group(conn: sqlite3.Connection, group: Group) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO groups (name, selector) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET selector = excluded.selector",
            (group.name, _dumps(group.selector)),
        )


def get_group(conn: sqlite3.Connection, name: str) -> Group | None:
    row = conn.execute("SELECT * FROM groups WHERE name = ?", (name,)).fetchone()
    return None if row is None else Group(row["name"], json.loads(row["selector"]))


def list_groups(conn: sqlite3.Connection) -> list[Group]:
    rows = conn.execute("SELECT * FROM groups ORDER BY name").fetchall()
    return [Group(r["name"], json.loads(r["selector"])) for r in rows]


def delete_group(conn: sqlite3.Connection, name: str) -> bool:
    """不檢查 route 參照(無外鍵,由 api.admin 拒絕,data.md)。"""
    with transaction(conn):
        cur = conn.execute("DELETE FROM groups WHERE name = ?", (name,))
    return cur.rowcount > 0


# ---------------------------------------------------------------- routes / route_targets


@dataclass(frozen=True)
class Route:
    name: str
    target: dict[str, Any]  # {group: name} | {tags: {all, any, none}}
    mode: str = "chat"
    tags: tuple[str, ...] = ()
    record_context: bool | None = None  # None = 沿用全域(spec 2.4.2)
    budget_precheck: bool = False


@dataclass(frozen=True)
class RouteTarget:
    deployment_id: str
    slot: int
    position: int
    effort: str | None = None
    weight: str = "medium"


def _route_from_row(row: sqlite3.Row) -> Route:
    return Route(
        name=row["name"],
        target=json.loads(row["target"]),
        mode=row["mode"],
        tags=tuple(json.loads(row["tags"])),
        record_context=_bool_or_none(row["record_context"]),
        budget_precheck=bool(row["budget_precheck"]),
    )


def save_route(conn: sqlite3.Connection, route: Route) -> None:
    """新增或更新 route;既有 route_targets 保留(以 `replace_route_targets` 另行取代)。"""
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO routes (name, target, mode, tags, record_context, budget_precheck)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
              target = excluded.target,
              mode = excluded.mode,
              tags = excluded.tags,
              record_context = excluded.record_context,
              budget_precheck = excluded.budget_precheck
            """,
            (
                route.name,
                _dumps(route.target),
                route.mode,
                _dumps(list(route.tags)),
                _bool_to_int(route.record_context),
                int(bool(route.budget_precheck)),
            ),
        )


def get_route(conn: sqlite3.Connection, name: str) -> Route | None:
    row = conn.execute("SELECT * FROM routes WHERE name = ?", (name,)).fetchone()
    return None if row is None else _route_from_row(row)


def list_routes(conn: sqlite3.Connection) -> list[Route]:
    return [_route_from_row(r) for r in conn.execute("SELECT * FROM routes ORDER BY name")]


def delete_route(conn: sqlite3.Connection, name: str) -> bool:
    """刪除 route;route_targets 由外鍵串聯刪除。"""
    with transaction(conn):
        cur = conn.execute("DELETE FROM routes WHERE name = ?", (name,))
    return cur.rowcount > 0


def list_route_targets(conn: sqlite3.Connection, route_name: str) -> list[RouteTarget]:
    """依 (slot, position, deployment_id) 排序。"""
    rows = conn.execute(
        "SELECT * FROM route_targets WHERE route_name = ? "
        "ORDER BY slot, position, deployment_id",
        (route_name,),
    ).fetchall()
    return [
        RouteTarget(
            deployment_id=r["deployment_id"],
            slot=r["slot"],
            position=r["position"],
            effort=r["effort"],
            weight=r["weight"],
        )
        for r in rows
    ]


def replace_route_targets(
    conn: sqlite3.Connection, route_name: str, rows: Iterable[RouteTarget]
) -> None:
    """在單一交易內刪除 route 的全部 route_targets 再寫入 `rows`。

    `rows` 在交易內才被逐筆取用;任何一筆失敗(CHECK、外鍵、或迭代本身丟例外)時
    整個交易回滾,舊資料完整保留(T14 order 儲存依賴此保證)。"""
    with transaction(conn):
        conn.execute("DELETE FROM route_targets WHERE route_name = ?", (route_name,))
        for t in rows:
            conn.execute(
                "INSERT INTO route_targets "
                "(route_name, deployment_id, slot, position, effort, weight) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (route_name, t.deployment_id, t.slot, t.position, t.effort, t.weight),
            )


# ---------------------------------------------------------------- usage_ledger / usage_payloads


@dataclass(frozen=True)
class LedgerRow:
    ts: datetime
    status: str  # ok | error | quota_exhausted | rate_limited | skipped_precheck
    route_name: str | None = None
    deployment_id: str | None = None
    effort: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    slot: int | None = None
    fallback_depth: int = 0
    latency_ms: int | None = None
    est_tokens_in: int | None = None
    est_tokens_out_p50: int | None = None
    est_tokens_out_p90: int | None = None
    est_cost_usd_p90: float | None = None
    error: str | None = None
    id: int | None = None


_LEDGER_COLS = (
    "ts",
    "route_name",
    "deployment_id",
    "effort",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "status",
    "slot",
    "fallback_depth",
    "latency_ms",
    "est_tokens_in",
    "est_tokens_out_p50",
    "est_tokens_out_p90",
    "est_cost_usd_p90",
    "error",
)


def insert_ledger(conn: sqlite3.Connection, row: LedgerRow) -> int:
    """寫入一列帳本,回傳新 id(`row.id` 忽略)。不存 prompt 或回應內容(spec 2.1.5)。"""
    values = [getattr(row, c) for c in _LEDGER_COLS]
    values[0] = to_iso(row.ts)
    with transaction(conn):
        cur = conn.execute(
            f"INSERT INTO usage_ledger ({', '.join(_LEDGER_COLS)}) "
            f"VALUES ({', '.join('?' for _ in _LEDGER_COLS)})",
            values,
        )
    return int(cur.lastrowid)


def _ledger_from_row(r: sqlite3.Row) -> LedgerRow:
    kw = {c: r[c] for c in _LEDGER_COLS}
    kw["ts"] = from_iso(kw["ts"])
    return LedgerRow(id=r["id"], **kw)


def get_ledger(conn: sqlite3.Connection, ledger_id: int) -> LedgerRow | None:
    r = conn.execute("SELECT * FROM usage_ledger WHERE id = ?", (ledger_id,)).fetchone()
    return None if r is None else _ledger_from_row(r)


def list_ledger(
    conn: sqlite3.Connection,
    *,
    route_name: str | None = None,
    deployment_id: str | None = None,
    status: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
    offset: int = 0,
    min_depth: int | None = None,
) -> list[LedgerRow]:
    """依條件列出帳本,新到舊(ts DESC, id DESC)。`since` 含、`until` 不含;`min_depth` 為
    `fallback_depth >= min_depth`(CHG-20261103-01 決策 10 ④)。"""
    where, args = _ledger_where(route_name, deployment_id, status, since, until, min_depth)
    sql = "SELECT * FROM usage_ledger" + where
    sql += " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?"
    args += [-1 if limit is None else limit, offset]
    return [_ledger_from_row(r) for r in conn.execute(sql, args)]


def _ledger_where(
    route_name: str | None,
    deployment_id: str | None,
    status: str | None,
    since: datetime | None,
    until: datetime | None,
    min_depth: int | None = None,
) -> tuple[str, list[Any]]:
    """`list_ledger` 與 `count_ledger` 共用的篩選條件:回傳 `(" WHERE …" 或 "", 參數)`。"""
    where: list[str] = []
    args: list[Any] = []
    for col, val in (("route_name", route_name), ("deployment_id", deployment_id), ("status", status)):
        if val is not None:
            where.append(f"{col} = ?")
            args.append(val)
    if since is not None:
        where.append("ts >= ?")
        args.append(to_iso(since))
    if until is not None:
        where.append("ts < ?")
        args.append(to_iso(until))
    if min_depth is not None:
        where.append("fallback_depth >= ?")
        args.append(min_depth)
    return (" WHERE " + " AND ".join(where) if where else ""), args


def count_ledger(
    conn: sqlite3.Connection,
    *,
    route_name: str | None = None,
    deployment_id: str | None = None,
    status: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    min_depth: int | None = None,
) -> int:
    """符合條件的帳本列數(條件同 `list_ledger`;管理 API 分頁的 `total`)。

    與 `list_ledger` 是兩條獨立敘述,呼叫端不包交易(CHG-20261101-02 決策審議 D25)。"""
    where, args = _ledger_where(route_name, deployment_id, status, since, until, min_depth)
    return int(conn.execute("SELECT COUNT(*) FROM usage_ledger" + where, args).fetchone()[0])


def iter_ledger(
    conn: sqlite3.Connection,
    *,
    route_name: str | None = None,
    deployment_id: str | None = None,
    status: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int,
    batch: int = 1000,
    min_depth: int | None = None,
) -> Iterator[list[LedgerRow]]:
    """依條件逐批讀出帳本(條件與順序同 `list_ledger`:新到舊),每批至多 `batch` 列、合計至多 `limit` 列。

    帳本 CSV 匯出用(CHG-20261102-02 決策 9):以 `fetchmany` 分批讀,不一次載入整個結果;
    整段在一個讀交易內(`BEGIN` … `COMMIT`),各批看到同一份快照。呼叫端在中途停止迭代
    (`close()`、例外)時回滾讀交易。呼叫端已在交易中時沿用,不另開也不提交。
    交易跨越多次 yield,所以不用 `read_tx`(context manager),在此自行 `BEGIN`(D27 串流例外;
    「已在交易內就沿用」的規則與 `read_tx` 相同)。"""
    where, args = _ledger_where(route_name, deployment_id, status, since, until, min_depth)
    sql = "SELECT * FROM usage_ledger" + where + " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(limit)
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    finished = False
    try:
        cur = conn.execute(sql, args)
        try:
            while True:
                rows = cur.fetchmany(batch)
                if not rows:
                    break
                yield [_ledger_from_row(r) for r in rows]
        finally:
            cur.close()
        finished = True
    finally:
        if own and conn.in_transaction:
            conn.execute("COMMIT" if finished else "ROLLBACK")


def payload_stats(conn: sqlite3.Connection) -> tuple[int, int]:
    """`usage_payloads` 的 `(列數, bytes 欄加總)`;沒有列時 `(0, 0)`(設定總覽,決策 10)。"""
    n, total = conn.execute("SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM usage_payloads").fetchone()
    return int(n), int(total)


def count_config(conn: sqlite3.Connection) -> dict[str, int]:
    """設定總覽的計數(決策 10):`deployments`、`groups`、`routes` 為各表列數,
    `ledger_ok` 為 `status = 'ok'` 的帳本列數。各一個 COUNT,不載入資料列。"""
    return {
        "deployments": int(conn.execute("SELECT COUNT(*) FROM deployments").fetchone()[0]),
        "groups": int(conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]),
        "routes": int(conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0]),
        "ledger_ok": int(conn.execute(
            "SELECT COUNT(*) FROM usage_ledger WHERE status = 'ok'").fetchone()[0]),
    }


def ledger_spend(
    conn: sqlite3.Connection,
    dep_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
) -> float:
    """成員的 `cost_usd` 加總(不分 status,NULL 不計;沒有列時 0.0)。

    `since` 為 None 時為累計;否則取 `ts` ∈ [since, until)(兩者須同時給)。
    ts 為固定格式 UTC 字串,可字典序比較。供 `QuotaService.remaining()`。"""
    sql = "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_ledger WHERE deployment_id = ?"
    args: list[Any] = [dep_id]
    if since is not None:
        assert until is not None
        sql += " AND ts >= ? AND ts < ?"
        args += [to_iso(since), to_iso(until)]
    return float(conn.execute(sql, args).fetchone()[0])


def recent_tokens_out(
    conn: sqlite3.Connection, dep_id: str, route_name: str | None, limit: int
) -> list[int]:
    """同 route 與成員最近 `limit` 筆 `status = 'ok'` 且 `tokens_out` 非 NULL 的 tokens_out(新到舊,
    `ts DESC, id DESC`)。`route_name=None` 以 `route_name IS NULL` 比對(`local/` 直呼);
    NULL 的 tokens_out 不佔名額。供 `Estimator.recent_tokens_out()`(spec 5.4.3)。"""
    sql = (
        "SELECT tokens_out FROM usage_ledger "
        "WHERE deployment_id = ? AND status = 'ok' AND tokens_out IS NOT NULL AND "
        + ("route_name IS NULL" if route_name is None else "route_name = ?")
        + " ORDER BY ts DESC, id DESC LIMIT ?"
    )
    args: list[Any] = [dep_id]
    if route_name is not None:
        args.append(route_name)
    args.append(limit)
    return [int(r[0]) for r in conn.execute(sql, args)]


# ---------------------------------------------------------------- dashboard 彙總(NFR-2)
# 查詢數與成員數無關:成員與 budgets 一次 JOIN、每種相異期間一次 GROUP BY 加總、近 24 小時
# 請求數與近 7 天花費合併一次 GROUP BY。不可改成逐成員查詢(CHG-20261101-02 決策審議 D24)。


def dashboard_members(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """每個現存成員一列(依 id),LEFT JOIN budgets 與 cooldowns(仍為單一查詢)。欄位:`id`、
    `model_name`、`kind`、`provider`、`enabled`、`b_type`、`limit_amount`、`window`、`reset_anchor`
    (budgets 欄位為原始值,沒有 budgets 列時為 NULL)、`has_budget`(0/1)、`cooling_until`、
    `cooling_reason`(cooldowns 原始值,不論是否到期;沒有冷卻列時為 NULL)。"""
    return conn.execute(
        "SELECT d.id, d.model_name, d.kind, d.provider, d.enabled, b.type AS b_type, "
        "b.limit_amount, b.window, b.reset_anchor, "
        "b.deployment_id IS NOT NULL AS has_budget, "
        "c.exhausted_until AS cooling_until, c.reason AS cooling_reason "
        "FROM deployments d LEFT JOIN budgets b ON b.deployment_id = d.id "
        "LEFT JOIN cooldowns c ON c.deployment_id = d.id ORDER BY d.id"
    ).fetchall()


def spend_by_deployment(
    conn: sqlite3.Connection, since: datetime | None, until: datetime | None
) -> dict[str | None, float]:
    """單一 `GROUP BY deployment_id` 查詢:各 deployment_id 的 `cost_usd` 加總(不分 status,
    NULL 不計)。`since` 為 None 時為累計,否則 `ts` ∈ [since, until)。含孤兒列與
    deployment_id 為 NULL 的列(鍵 None),由呼叫端只取現存成員。"""
    sql = "SELECT deployment_id, COALESCE(SUM(cost_usd), 0) FROM usage_ledger"
    args: list[Any] = []
    if since is not None:
        assert until is not None
        sql += " WHERE ts >= ? AND ts < ?"
        args = [to_iso(since), to_iso(until)]
    sql += " GROUP BY deployment_id"
    return {d: float(s) for d, s in conn.execute(sql, args)}


def recent_activity(
    conn: sqlite3.Connection, *, since_24h: datetime, since_7d: datetime
) -> dict[str | None, tuple[int, float]]:
    """單一 `GROUP BY deployment_id` 查詢:`{deployment_id: (近 24 小時列數, 近 7 天花費)}`。

    只含 `ts ≥ since_7d` 有列的 deployment_id;列數只計 `ts ≥ since_24h` 且 status 不是
    `skipped_precheck`(沒有送出請求)的列;花費不分 status、NULL 不計。"""
    return {
        d: (int(n24), float(s7))
        for d, n24, s7 in conn.execute(
            "SELECT deployment_id, "
            "SUM(CASE WHEN ts >= ? AND status != ? THEN 1 ELSE 0 END), "
            "COALESCE(SUM(cost_usd), 0) "
            "FROM usage_ledger WHERE ts >= ? GROUP BY deployment_id",
            (to_iso(since_24h), STATUS_SKIPPED_PRECHECK, to_iso(since_7d)),
        )
    }


@dataclass(frozen=True)
class Payload:
    ledger_id: int
    ts: datetime
    request_json: str  # 已序列化、已遮罩的請求內容(由 T10 產生)
    bytes: int
    response_text: str | None = None
    truncated: bool = False
    redacted: bool = False


def insert_payload(conn: sqlite3.Connection, p: Payload) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO usage_payloads "
            "(ledger_id, ts, request_json, response_text, bytes, truncated, redacted) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                p.ledger_id,
                to_iso(p.ts),
                p.request_json,
                p.response_text,
                p.bytes,
                int(bool(p.truncated)),
                int(bool(p.redacted)),
            ),
        )


def get_payload(conn: sqlite3.Connection, ledger_id: int) -> Payload | None:
    r = conn.execute("SELECT * FROM usage_payloads WHERE ledger_id = ?", (ledger_id,)).fetchone()
    if r is None:
        return None
    return Payload(
        ledger_id=r["ledger_id"],
        ts=from_iso(r["ts"]),
        request_json=r["request_json"],
        response_text=r["response_text"],
        bytes=r["bytes"],
        truncated=bool(r["truncated"]),
        redacted=bool(r["redacted"]),
    )


def delete_payloads_before(conn: sqlite3.Connection, cutoff: datetime) -> int:
    """刪除 `ts` 早於 `cutoff` 的 payload(spec 2.4.5),回傳刪除列數;帳本列保留。"""
    with transaction(conn):
        cur = conn.execute("DELETE FROM usage_payloads WHERE ts < ?", (to_iso(cutoff),))
    return cur.rowcount


# ---------------------------------------------------------------- settings

# spec 2.4.1、2.4.5、2.4.3;資料庫缺鍵時取用,不寫入(決策 24)
SETTING_DEFAULTS: dict[str, str] = {
    "record_context_default": "1",
    "context_retention_days": "7",
    "context_max_bytes": "262144",
}


@dataclass(frozen=True)
class ContextSettings:
    record_default: bool
    retention_days: int
    max_bytes: int


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    """回傳字串值;缺鍵時回傳 `SETTING_DEFAULTS` 的值,沒有預設值的鍵回 None。"""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is not None:
        return row[0]
    return SETTING_DEFAULTS.get(key)


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """寫入或覆寫。bool 存成 `"1"`/`"0"`(與 `get_context_settings` 的讀法一致),
    其餘以 `str(value)` 存。"""
    text = ("1" if value else "0") if isinstance(value, bool) else str(value)
    with transaction(conn):
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, text),
        )


def get_context_settings(conn: sqlite3.Connection) -> ContextSettings:
    """context 記錄的三個設定,已轉型;缺鍵取常數預設值。"""
    return ContextSettings(
        record_default=get_setting(conn, "record_context_default") == "1",
        retention_days=int(get_setting(conn, "context_retention_days")),
        max_bytes=int(get_setting(conn, "context_max_bytes")),
    )


# ---------------------------------------------------------------- config 匯出讀取(CHG-20261102-02 T3)
# `spillway.configio.dump` 用:整份設定在同一個讀交易(快照)內讀出;本段只讀不寫。


@contextmanager
def read_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """讀交易(CHG-20261103-01 決策 21 ④):已在交易內(`conn.in_transaction`,含 `transaction(conn)` 的
    寫鎖或外層 `read_tx`)時直接 yield,不 BEGIN、不 ROLLBACK,外層交易照常由外層結束;否則 `BEGIN`
    (DEFERRED,不取寫鎖;SQLite 不會強制唯讀,呼叫端只做讀取)… 結束時一律 `ROLLBACK`(只讀,結果與
    COMMIT 相同)。WAL 模式下區塊內的多條 SELECT 看到同一份快照。
    `iter_ledger` 是 generator,交易跨越多次 yield,仍在函式內自行 `BEGIN`(D27 串流例外)。"""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """單一讀交易(`read_tx`):整份設定在同一份快照內讀出。"""
    with read_tx(conn):
        yield conn


def list_all_route_targets(conn: sqlite3.Connection) -> dict[str, list[RouteTarget]]:
    """全部 route 的 route_targets,單一查詢;`{route_name: [RouteTarget…]}`,各 route 內依
    `(slot, position, deployment_id)` 排序(同 `list_route_targets`);沒有手動排序的 route 不出現。"""
    out: dict[str, list[RouteTarget]] = {}
    for r in conn.execute(
        "SELECT * FROM route_targets ORDER BY route_name, slot, position, deployment_id"
    ):
        out.setdefault(r["route_name"], []).append(RouteTarget(
            deployment_id=r["deployment_id"], slot=r["slot"], position=r["position"],
            effort=r["effort"], weight=r["weight"],
        ))
    return out


# ---------------------------------------------------------------- config 匯入寫入(CHG-20261102-02 T4)
# `spillway.configio.apply` 用(呼叫端已在 `transaction(conn)` 內)。實體的刪除沿用 `delete_route`、
# `delete_group`、`delete_deployment`(逐筆;不整表 DELETE)。


def delete_settings(conn: sqlite3.Connection, keys: Iterable[str]) -> None:
    """刪除指定的 settings 鍵(之後讀取回到 `SETTING_DEFAULTS`);不存在的鍵略過。禁止整表 DELETE
    (存取 token 雜湊與其他設定同表)。"""
    with transaction(conn):
        conn.executemany("DELETE FROM settings WHERE key = ?", [(k,) for k in keys])


def deployment_ids_by_provider(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """`{provider: [成員 id…]}`(各清單依 id 排序);匯入後的 cardinality 檢查用。"""
    out: dict[str, list[str]] = {}
    for provider, dep_id in conn.execute("SELECT provider, id FROM deployments ORDER BY provider, id"):
        out.setdefault(provider, []).append(dep_id)
    return out

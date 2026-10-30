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
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from spillway.domain import Budget, Deployment
from spillway.clock import from_iso, to_iso
from spillway.store.db import transaction

# ---------------------------------------------------------------- 共用轉換


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else to_iso(dt)


def _dt(s: str | None) -> datetime | None:
    return None if s is None else from_iso(s)


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
        exhausted_until=_dt(row["exhausted_until"]),
    )


def save_budget(conn: sqlite3.Connection, dep_id: str, budget: Budget | None) -> None:
    """新增或取代成員的 budgets 列;`None` 刪除該列。"""
    with transaction(conn):
        if budget is None:
            conn.execute("DELETE FROM budgets WHERE deployment_id = ?", (dep_id,))
            return
        conn.execute(
            """
            INSERT INTO budgets (deployment_id, type, limit_amount, window, reset_anchor, exhausted_until)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(deployment_id) DO UPDATE SET
              type = excluded.type,
              limit_amount = excluded.limit_amount,
              window = excluded.window,
              reset_anchor = excluded.reset_anchor,
              exhausted_until = excluded.exhausted_until
            """,
            (
                dep_id,
                budget.type,
                budget.limit_amount,
                budget.window,
                budget.reset_anchor,
                _iso(budget.exhausted_until),
            ),
        )


def set_exhausted_until(conn: sqlite3.Connection, dep_id: str, when: datetime | None) -> bool:
    """設定或清除(None)冷卻期限;成員沒有 budgets 列時不建立,回傳 False。"""
    with transaction(conn):
        cur = conn.execute(
            "UPDATE budgets SET exhausted_until = ? WHERE deployment_id = ?",
            (_iso(when), dep_id),
        )
    return cur.rowcount > 0


def mark_exhausted_until(conn: sqlite3.Connection, dep_id: str, when: datetime) -> None:
    """單一原子敘述設定冷卻;沒有 budgets 列時插入 type=none、window=none 的列,
    已有列時只改 exhausted_until(不動 type、limit_amount、window、reset_anchor)。"""
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO budgets (deployment_id, type, limit_amount, window, exhausted_until)
            VALUES (?, 'none', NULL, 'none', ?)
            ON CONFLICT(deployment_id) DO UPDATE SET exhausted_until = excluded.exhausted_until
            """,
            (dep_id, _iso(when)),
        )


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
) -> list[LedgerRow]:
    """依條件列出帳本,新到舊(ts DESC, id DESC)。`since` 含、`until` 不含。"""
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
    sql = "SELECT * FROM usage_ledger"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?"
    args += [-1 if limit is None else limit, offset]
    return [_ledger_from_row(r) for r in conn.execute(sql, args)]


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

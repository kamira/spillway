"""測試控制介面 `/__test__/*`(CHG-20261107-03 決策 5–7;spec「測試控制介面(僅測試模式)」)。

**僅供測試。** 只有 `create_app(test_control=True)` 會以 `include_in_schema=False` 掛載 `make_router(rng_seed=…)`
建出的 router(每個 app 一個;seed 由閉包持有,不新增 `app.state` 屬性);
`spillway serve` 只在環境變數 `SPILLWAY_TEST_CONTROL=1`(且 `SPILLWAY_HOME` 明確設定、不是預設資料
目錄)時傳入 True——讀環境變數的只有 `cli._run_server`,本模組與 `create_app` 都不讀 `os.environ`。
未掛載時所有 `/__test__/*` 與其他不存在的路徑一樣回 404。

共同規則:JSON;錯誤沿用 spec 9.1.2 `{"error": {"code", "message"}}`;body 不接受未知欄位(422
`validation_error`);時間一律 `YYYY-MM-DDTHH:MM:SSZ`(其他格式 422);所有 GET 不改變資料庫狀態。
錯誤碼:404 `not_found`(成員或帳本列不存在)、409 `clock_not_controllable`(系統時鐘模式下要推進時鐘)、
409 `retention_not_stepped`(非 STEPPED 模式呼叫 tick)、409 `retention_not_running`(排程未執行)、
409 `cli_slots_exceeded`(acquire 超過 limit)、409 `cli_slots_underflow`(release 多於進行中數量)、
422 `validation_error`(格式錯誤或資料庫約束違反)。

端點:
- `GET /__test__/clock` → `{controllable, now}`;`POST /__test__/clock` `{"set": t}` 或
  `{"advance_seconds": n}`(擇一;n 為非負整數)→ `{now}`。可控時鐘凍結,只有這兩者讓它改變。
- `GET /__test__/rng` → `{seeded, seed}`(seed 為不透明值)。
- `POST /__test__/ledger` `{"rows": [...]}`、`POST /__test__/payloads` `{"rows": [...]}`:upsert,
  回 `{ids}`。種入不經 Redactor。
- `POST /__test__/cooldowns` `{"deployment", "until", "reason"}`:覆寫(不比較期限)→ 回同樣三欄。
- `POST /__test__/raw/deployment-params` `{"id", "params"}`:只更新 `params` 欄、不驗證,回 `{id}`;
  不回顯、不記錄 params。
- `POST /__test__/retention/run` → `{deleted}`(null 表示清理失敗);`GET /__test__/retention` →
  `{runs, next_due}`;`POST /__test__/retention/tick` `{"advance_seconds"}`(STEPPED 才可用)→
  `{ran, deleted, runs, now}`。
- `GET /__test__/health-cache` → `{entries: [{deployment, healthy}]}`(TTL 內的項目,依 deployment 排序)。
- `GET /__test__/cli-slots` → `{slots: [{deployment, in_flight, limit}]}`(全部 `kind=cli` 成員,依 id 排序);
  `POST /__test__/cli-slots/acquire`、`/release` `{"deployment", "n"}` → `{deployment, in_flight, limit}`。
- `GET /__test__/mcp/sessions/{id}` → `{stream_open}`。

秘密:本模組不記錄任何請求或回應內容;回應不含 token 或憑證。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, StrictInt

from spillway.api import ApiError
from spillway.api.deps import request_conn
from spillway.api.mcp.sdk_compat import get_stream_open
from spillway.clock import FakeClock, from_iso, to_iso
from spillway.routing.filter import max_concurrency
from spillway.store import repo
from spillway.store.db import (
    seed_ledger_rows,
    seed_payload_rows,
    set_deployment_params_raw,
    transaction,
)

__all__ = ["PREFIX", "make_router"]

PREFIX = "/__test__"

_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _parse_time(value: Any) -> datetime:
    """只接受 `YYYY-MM-DDTHH:MM:SSZ` 字串;其他型別、其他格式與不存在的日期(例如 13 月)都丟 ValueError → 422。"""
    if not isinstance(value, str) or not _TIME_RE.fullmatch(value):
        raise ValueError("時間必須是 YYYY-MM-DDTHH:MM:SSZ")
    return from_iso(value)


# spec 2.1.2 的時間格式
Time = Annotated[datetime, BeforeValidator(_parse_time)]
NonNegInt = Annotated[StrictInt, Field(ge=0)]
PosInt = Annotated[StrictInt, Field(ge=1)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- 請求 body


class ClockIn(_Body):
    set: Time | None = None
    advance_seconds: NonNegInt | None = None


class LedgerSeed(_Body):
    id: PosInt | None = None
    ts: Time
    status: str
    route_name: str | None = None
    deployment_id: str | None = None
    effort: str | None = None
    tokens_in: StrictInt | None = None
    tokens_out: StrictInt | None = None
    cost_usd: float | None = None
    slot: StrictInt | None = None
    fallback_depth: NonNegInt = 0
    latency_ms: StrictInt | None = None
    est_tokens_in: StrictInt | None = None
    est_tokens_out_p50: StrictInt | None = None
    est_tokens_out_p90: StrictInt | None = None
    est_cost_usd_p90: float | None = None
    error: str | None = None


class LedgerIn(_Body):
    rows: list[LedgerSeed]


class PayloadSeed(_Body):
    ledger_id: PosInt
    ts: Time
    request_json: str
    response_text: str | None = None
    bytes: NonNegInt
    truncated: StrictBool = False
    redacted: StrictBool = False


class PayloadsIn(_Body):
    rows: list[PayloadSeed]


class CooldownIn(_Body):
    deployment: str
    until: Time
    reason: Literal["quota_exhausted", "rate_limited"]


class RawParamsIn(_Body):
    id: str
    params: Any


class TickIn(_Body):
    advance_seconds: NonNegInt


class SlotsIn(_Body):
    deployment: str
    n: PosInt


# ---------------------------------------------------------------- 輔助


def _controllable(request: Request) -> FakeClock:
    clock = request.app.state.clock
    if not isinstance(clock, FakeClock):
        raise ApiError(409, "clock_not_controllable",
                       "目前是系統時鐘,無法設定或推進(未設 SPILLWAY_TEST_START_TIME)")
    return clock


def _advance(request: Request, seconds: int) -> None:
    if seconds:
        _controllable(request).advance(seconds=seconds)


def _row_dict(model: BaseModel) -> dict[str, Any]:
    out = model.model_dump()
    out["ts"] = to_iso(out["ts"])
    for key in ("truncated", "redacted"):
        if key in out:
            out[key] = int(out[key])
    return out


def _constraint_error(exc: sqlite3.IntegrityError) -> ApiError:
    # 只回例外型別,不回顯種入內容
    return ApiError(422, "validation_error", f"資料庫約束違反({type(exc).__name__})")


def _slot(runner: Any, dep: Any) -> dict[str, Any]:
    return {"deployment": dep.id, "in_flight": runner.in_flight(dep.id), "limit": max_concurrency(dep)}


def _cli_member(request: Request, dep_id: str) -> Any:
    with request_conn(request) as conn:
        dep = repo.load_deployment(conn, dep_id)
    if dep is None or dep.kind != "cli":
        raise ApiError(404, "not_found", "CLI 成員不存在")
    return dep


# ---------------------------------------------------------------- router


def make_router(*, rng_seed: int | None = None) -> APIRouter:
    """建一個 `/__test__` router(每個 app 一個);`rng_seed` 為啟動時的亂數種子,由 `GET /__test__/rng`
    回報(未設定為 None)。"""
    router = APIRouter(prefix=PREFIX)

    # ---- 時鐘與亂數(決策 7 ①②)

    @router.get("/clock")
    async def get_clock(request: Request) -> dict[str, Any]:
        clock = request.app.state.clock
        return {"controllable": isinstance(clock, FakeClock), "now": to_iso(clock.now())}

    @router.post("/clock")
    async def post_clock(request: Request, body: ClockIn) -> dict[str, Any]:
        if (body.set is None) == (body.advance_seconds is None):
            raise ApiError(422, "validation_error", "body 必須剛好含 set 或 advance_seconds 其中一個")
        clock = _controllable(request)
        if body.set is not None:
            clock.set(body.set)
        else:
            clock.advance(seconds=body.advance_seconds or 0)
        return {"now": to_iso(clock.now())}

    @router.get("/rng")
    async def get_rng() -> dict[str, Any]:
        return {"seeded": rng_seed is not None, "seed": rng_seed}

    # ---- 帳本與內容(決策 7 ③;種入不經 Redactor)

    @router.post("/ledger")
    async def post_ledger(request: Request, body: LedgerIn) -> dict[str, Any]:
        rows = [_row_dict(r) for r in body.rows]
        with request_conn(request) as conn:
            try:
                ids = seed_ledger_rows(conn, rows)
            except sqlite3.IntegrityError as exc:
                raise _constraint_error(exc) from None
        return {"ids": ids}

    @router.post("/payloads")
    async def post_payloads(request: Request, body: PayloadsIn) -> dict[str, Any]:
        rows = [_row_dict(r) for r in body.rows]
        with request_conn(request) as conn:
            try:
                ids = seed_payload_rows(conn, rows)
            except sqlite3.IntegrityError as exc:
                raise _constraint_error(exc) from None
        return {"ids": ids}

    # ---- 冷卻與損壞狀態(決策 7 ④⑤)

    @router.post("/cooldowns")
    async def post_cooldown(request: Request, body: CooldownIn) -> dict[str, Any]:
        with request_conn(request) as conn:
            if not repo.deployment_exists(conn, body.deployment):
                raise ApiError(404, "not_found", "成員不存在")
            # 覆寫:mark_cooldown 只延長,所以在同一交易內先清除再設定
            with transaction(conn):
                repo.clear_cooldown(conn, body.deployment)
                repo.mark_cooldown(conn, body.deployment, body.until, body.reason)
        return {"deployment": body.deployment, "until": to_iso(body.until), "reason": body.reason}

    @router.post("/raw/deployment-params")
    async def post_raw_deployment_params(request: Request, body: RawParamsIn) -> dict[str, Any]:
        # 繞過驗證:只寫 params 欄;不回顯、不記錄 params(測試值一律 sk-fake…)
        params_json = json.dumps(body.params, ensure_ascii=False, sort_keys=True)
        with request_conn(request) as conn:
            if not set_deployment_params_raw(conn, body.id, params_json):
                raise ApiError(404, "not_found", "成員不存在")
        return {"id": body.id}

    # ---- 保存清理(決策 7 ⑥)

    @router.post("/retention/run")
    async def post_retention_run(request: Request) -> dict[str, Any]:
        return {"deleted": request.app.state.retention.run_once()}

    @router.get("/retention")
    async def get_retention(request: Request) -> dict[str, Any]:
        retention = request.app.state.retention
        due = retention.next_due
        return {"runs": retention.runs, "next_due": None if due is None else to_iso(due)}

    @router.post("/retention/tick")
    async def post_retention_tick(request: Request, body: TickIn) -> dict[str, Any]:
        retention = request.app.state.retention
        if not retention.stepped:
            raise ApiError(409, "retention_not_stepped",
                           "保存排程不是 STEPPED 模式(未設 SPILLWAY_TEST_RETENTION_STEPPED=1)")
        if retention.task is None or retention.task.done():
            raise ApiError(409, "retention_not_running", "保存排程未執行")
        try:
            # ① 推進時鐘 ② 放行 _loop 一次 ③ 等它回到閘前;三者在 step 的鎖內依序執行,並行的 tick 不交錯
            ran, deleted = await retention.step(before=lambda: _advance(request, body.advance_seconds))
        except RuntimeError:
            raise ApiError(409, "retention_not_running", "保存排程未執行") from None
        return {"ran": ran, "deleted": deleted, "runs": retention.runs,
                "now": to_iso(request.app.state.clock.now())}

    # ---- 健康快取、CLI 名額、MCP(決策 7 ⑦⑧⑨)

    @router.get("/health-cache")
    async def get_health_cache(request: Request) -> dict[str, Any]:
        cache = request.app.state.router.health_cache
        entries = []
        for dep_id in sorted(cache._data):   # HealthCache 沒有列舉 API;`get()` 套用 TTL(過期的不列出)
            healthy = cache.get(dep_id)
            if healthy is not None:
                entries.append({"deployment": dep_id, "healthy": healthy})
        return {"entries": entries}

    @router.get("/cli-slots")
    async def get_cli_slots(request: Request) -> dict[str, Any]:
        runner = request.app.state.cli_runner
        with request_conn(request) as conn:
            deps = [d for d in repo.list_deployments(conn) if d.kind == "cli"]
        return {"slots": [_slot(runner, d) for d in sorted(deps, key=lambda d: d.id)]}

    @router.post("/cli-slots/acquire")
    async def post_cli_slots_acquire(request: Request, body: SlotsIn) -> dict[str, Any]:
        dep = _cli_member(request, body.deployment)
        runner = request.app.state.cli_runner
        limit = max_concurrency(dep)
        if runner.in_flight(dep.id) + body.n > limit:   # 全有或全無
            raise ApiError(409, "cli_slots_exceeded", f"佔用後會超過上限 {limit}")
        for _ in range(body.n):
            runner.try_acquire(dep.id, limit)
        return _slot(runner, dep)

    @router.post("/cli-slots/release")
    async def post_cli_slots_release(request: Request, body: SlotsIn) -> dict[str, Any]:
        dep = _cli_member(request, body.deployment)
        runner = request.app.state.cli_runner
        if body.n > runner.in_flight(dep.id):
            raise ApiError(409, "cli_slots_underflow", "釋放數量多於進行中數量")
        for _ in range(body.n):
            runner.release(dep.id)
        return _slot(runner, dep)

    @router.get("/mcp/sessions/{session_id}")
    async def get_mcp_session(request: Request, session_id: str) -> dict[str, Any]:
        return {"stream_open": get_stream_open(request.app, session_id)}

    return router

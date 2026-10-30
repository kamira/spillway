"""管理 API `/api/*`(spec 9.2 本筆範圍、9.2.1、9.2.2、9.2.3、9.3;CHG-20261030-02 T14;
design D13、D15、D22;決策 6、16、17、22、24、25、32)。

掛載:`app.include_router(admin.router)`(T15)。本模組只匯出 `router`,不修改 `app.py`。

依賴(一律從 `request.app.state` 取得,與 T13 `create_app` 放的屬性名一致):
- `registry`:`AdapterRegistry`(已 `discover()`,帶 `AdapterContext`)
- `router`:`routing.Router`(dry-run 與 `/api/estimate` 用)
- `conn_factory`:零參數函式,每次呼叫回傳**新開**的 `sqlite3.Connection`;本模組每個請求呼叫一次、
  請求結束時關閉(與 `Router(..., close_conn=True)` 的假設相同)
- `clock`:`spillway.clock.Clock`

錯誤(spec 9.1.2;決策 16、17、22):一律 `{"error": {"code", "message"}}`。
- 422 `validation_error`:body/query/path 驗證失敗(含 FastAPI 的 `RequestValidationError`,
  由本 router 的 `_AdminRoute` 轉換,不依賴 app 層 handler);message 為「欄位路徑: 說明」,
  多筆以「; 」連接,路徑以 `.` 連接(例:`body.capabilities.efforts.1`)。**不回顯輸入值**。
- 404 `not_found`:資源不存在;payload 從未記錄或帳本列不存在。
- 409 `conflict`:id/name 重複;刪除仍被 route 參照的群組。
- 409 `provider_single`:`single` 模組的 provider 已有成員(POST,或 PUT 變更 provider)。
- 422 `member_not_in_target`:`PUT /api/routes/{name}/order` 的成員不屬 route 目標(9.3.1)。
- 410 `payload_expired`:payload 已過保存期限(決策 17)。

寫入驗證(決策 22):`credential_ref` 只接受 `env:…`、`keyring:…:…`(`credentials.validate_ref`);
`params` 內任何字串(含巢狀與鍵名)命中 `Redactor.matches` 即 422;`reset_anchor` 1–28;
`kind`、`effort`、`weight`、`mode`、`window`、budget `type` 只接受 DDL 的合法值;
`capabilities.efforts` 只可含五級。成員 id、群組名不可含空白與 `/`;route 名另不可含 `:`
(避免與 `local/`、`route:` 前綴混淆)。各 body 不接受未知欄位(`/api/estimate`、dry-run 的請求 body
除外,其餘 OpenAI 參數原樣轉交)。

各端點回應格式見各函式 docstring。時間一律 UTC ISO 8601 到秒(`2026-10-30T12:00:00Z`)。
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
)

from spillway import credentials
from spillway.adapters.base import (
    AdapterContext,
    AdapterUnavailableError,
    ModuleInfo,
    available_models,
)
from spillway.clock import to_iso
from spillway.domain import CALL_STATUSES, Budget, CallResult, ChatRequest, Deployment
from spillway.estimate import Estimate, Estimator
from spillway.ledger import LEDGER_STATUSES, Attempt, LedgerNotFound, LedgerWriter, payload_status
from spillway.quota import QuotaService
from spillway.redact import Redactor
from spillway.routing import (
    CLOUD_KINDS,
    InvalidEffort,
    Plan,
    RouteNotFound,
    members_of,
    normalize_effort,
)
from spillway.store import repo
from spillway.store.db import transaction

__all__ = ["router", "ApiError", "LEDGER_PAGE_SIZE", "LEDGER_PAGE_SIZE_MAX",
           "CONTEXT_MAX_BYTES_MIN", "TEST_PROMPT"]

LEDGER_PAGE_SIZE = 50
LEDGER_PAGE_SIZE_MAX = 500
CONTEXT_MAX_BYTES_MIN = 1024     # context_max_bytes 下限:小於 1 KiB 的 payload 已無檢視價值
TEST_PROMPT = "ping"
TEST_MAX_TOKENS = 16
TEST_TEXT_PREVIEW = 200          # 測試呼叫回傳的文字預覽上限(字元)

_REDACTOR = Redactor()


# ================================================================ 錯誤處理


class ApiError(Exception):
    """以 9.1.2 格式回應的錯誤。message 不可含秘密或使用者輸入的原值。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def _format_validation(errors: list[Mapping[str, Any]]) -> str:
    """`loc: msg; …`。只用 pydantic 的 `loc` 與 `msg`(不含 `input`,避免回顯輸入值)。"""
    parts = []
    for e in errors:
        loc = ".".join(str(x) for x in e.get("loc", ()))
        msg = str(e.get("msg", "格式錯誤"))
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) or "格式錯誤"


def _invalid(path: str, message: str) -> ApiError:
    return ApiError(422, "validation_error", f"{path}: {message}")


def _not_found(what: str) -> ApiError:
    return ApiError(404, "not_found", f"找不到{what}")


class _AdminRoute(APIRoute):
    """把本 router 內的 `RequestValidationError` 與 `ApiError` 轉成 9.1.2 格式。

    exception handler 只能掛在 app 上;以 route class 處理,admin.router 掛到任何 app 都一致。
    """

    def get_route_handler(self) -> Callable[[Request], Any]:
        handler = super().get_route_handler()

        async def wrapped(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                return _error_response(422, "validation_error", _format_validation(exc.errors()))
            except ApiError as exc:
                return _error_response(exc.status, exc.code, exc.message)

        return wrapped


router = APIRouter(prefix="/api", route_class=_AdminRoute)


# ================================================================ 共用


@contextmanager
def _db(request: Request) -> Iterator[sqlite3.Connection]:
    conn = request.app.state.conn_factory()
    try:
        yield conn
    finally:
        conn.close()


def _clock(request: Request) -> Any:
    return request.app.state.clock


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else to_iso(dt)


def _estimate_out(est: Estimate | None) -> dict[str, Any] | None:
    return None if est is None else dataclasses.asdict(est)


EffortLit = Literal["none", "low", "medium", "high", "max"]
WeightLit = Literal["high", "medium", "low"]
Tag = Annotated[str, Field(min_length=1, max_length=200)]
_ID_PATTERN = r"^[^\s/]+$"
_ROUTE_PATTERN = r"^[^\s/:]+$"


# ================================================================ 請求 body:成員


class BudgetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["usd", "credit_pool", "none"]
    limit_amount: float | None = Field(default=None, ge=0)    # NULL = 無上限(決策 6)
    window: Literal["monthly", "weekly", "daily", "none"] = "monthly"
    reset_anchor: StrictInt | None = Field(default=None, ge=1, le=28)


class CapabilitiesIn(BaseModel):
    """`{ctx, tools, vision, efforts[]}`;另有其他鍵時原樣保存。"""

    model_config = ConfigDict(extra="allow")

    ctx: StrictInt | None = Field(default=None, ge=1)
    tools: StrictBool | None = None
    vision: StrictBool | None = None
    efforts: list[EffortLit] | None = None


def _secret_paths(value: Any, path: str) -> list[str]:
    """`value` 內命中 Redactor 樣式的位置(鍵名本身命中時以 `<key>` 代替,不回顯)。"""
    hits: list[str] = []
    if isinstance(value, str):
        if _REDACTOR.matches(value):
            hits.append(path)
    elif isinstance(value, Mapping):
        for k, v in value.items():
            ks = str(k)
            if _REDACTOR.matches(ks):
                hits.append(f"{path}.<key>")
                continue
            hits += _secret_paths(v, f"{path}.{ks}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            hits += _secret_paths(v, f"{path}.{i}")
    return hits


class DeploymentIn(BaseModel):
    """POST 與 PUT 共用;POST 必須帶 `id`,PUT 的 `id` 可省略,給了就必須與路徑相同。"""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, min_length=1, max_length=200, pattern=_ID_PATTERN)
    model_name: str = Field(min_length=1)
    kind: Literal["cli", "api", "local"]
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    credential_ref: str | None = None
    capabilities: CapabilitiesIn = Field(default_factory=CapabilitiesIn)
    enabled: StrictBool = True
    tags: list[Tag] = Field(default_factory=list)
    budget: BudgetIn | None = None

    @field_validator("credential_ref")
    @classmethod
    def _check_ref(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            credentials.validate_ref(v)
        except credentials.CredentialError:
            raise ValueError("格式無效,必須為 env:變數名 或 keyring:服務:帳號") from None
        return v

    @field_validator("params")
    @classmethod
    def _check_params(cls, v: dict[str, Any]) -> dict[str, Any]:
        hits = _secret_paths(v, "params")
        if hits:
            raise ValueError(
                f"疑似金鑰,請改用 credential_ref(位置:{', '.join(hits)})")
        return v

    def to_deployment(self, dep_id: str, exhausted_until: datetime | None) -> Deployment:
        budget = None
        if self.budget is not None:
            b = self.budget
            budget = Budget(type=b.type, limit_amount=b.limit_amount, window=b.window,
                            reset_anchor=b.reset_anchor, exhausted_until=exhausted_until)
        return Deployment(
            id=dep_id, model_name=self.model_name, kind=self.kind, provider=self.provider,
            model=self.model, params=dict(self.params), credential_ref=self.credential_ref,
            capabilities=self.capabilities.model_dump(exclude_unset=True),
            enabled=self.enabled, tags=tuple(sorted(set(self.tags))), budget=budget,
        )


def _dep_out(d: Deployment) -> dict[str, Any]:
    """成員回應。只含 `credential_ref`(參照),不解析、不含任何憑證值(spec 2.1.3、3.1.1)。"""
    b = d.budget
    return {
        "id": d.id,
        "model_name": d.model_name,
        "kind": d.kind,
        "provider": d.provider,
        "model": d.model,
        "params": d.params,
        "credential_ref": d.credential_ref,
        "capabilities": d.capabilities,
        "enabled": d.enabled,
        "tags": list(d.tags),
        "budget": None if b is None else {
            "type": b.type, "limit_amount": b.limit_amount, "window": b.window,
            "reset_anchor": b.reset_anchor, "exhausted_until": _iso(b.exhausted_until),
        },
    }


def _load_dep(conn: sqlite3.Connection, dep_id: str) -> Deployment:
    dep = repo.load_deployment(conn, dep_id)
    if dep is None:
        raise _not_found(f"成員 {dep_id}")
    return dep


def _check_cardinality(conn: sqlite3.Connection, registry: Any, dep: Deployment) -> None:
    """spec 7.6.5、2.1.4、決策 22:呼叫端必須已在 `transaction(conn)` 內(BEGIN IMMEDIATE 取得寫鎖,
    查詢與寫入之間不會有別的連線插入成員)。"""
    if registry.cardinality_of(dep.provider, dep.kind) != "single":
        return
    rows = conn.execute(
        "SELECT id FROM deployments WHERE provider = ? AND id != ? ORDER BY id",
        (dep.provider, dep.id),
    ).fetchall()
    if rows:
        raise ApiError(
            409, "provider_single",
            f"provider {dep.provider} 的模組僅允許 1 個成員,已有 1 個:{rows[0][0]}")


# ================================================================ /api/deployments


@router.get("/deployments")
async def list_deployments(request: Request) -> dict[str, Any]:
    """`{"deployments": [成員, …]}`(依 id 排序;成員格式同 `GET /api/deployments/{id}`)。"""
    with _db(request) as conn:
        return {"deployments": [_dep_out(d) for d in repo.list_deployments(conn)]}


@router.post("/deployments", status_code=201)
async def create_deployment(request: Request, body: DeploymentIn) -> dict[str, Any]:
    """建立成員(含 tags、budget),回 201 與成員。重複 id → 409 `conflict`;
    `single` 模組的 provider 已有成員 → 409 `provider_single`(同一交易內檢查)。"""
    if body.id is None:
        raise _invalid("body.id", "Field required")
    dep = body.to_deployment(body.id, None)
    with _db(request) as conn:
        with transaction(conn):
            if conn.execute("SELECT 1 FROM deployments WHERE id = ?", (dep.id,)).fetchone():
                raise ApiError(409, "conflict", f"成員 id 已存在:{dep.id}")
            _check_cardinality(conn, request.app.state.registry, dep)
            repo.save_deployment(conn, dep)
        return _dep_out(_load_dep(conn, dep.id))


@router.get("/deployments/{dep_id}")
async def get_deployment(request: Request, dep_id: str) -> dict[str, Any]:
    """單一成員:`id`、`model_name`、`kind`、`provider`、`model`、`params`、`credential_ref`、
    `capabilities`、`enabled`、`tags[]`、`budget`(`{type, limit_amount, window, reset_anchor,
    exhausted_until}` 或 null)。"""
    with _db(request) as conn:
        return _dep_out(_load_dep(conn, dep_id))


@router.put("/deployments/{dep_id}")
async def update_deployment(request: Request, dep_id: str, body: DeploymentIn) -> dict[str, Any]:
    """整筆取代(tags、budget 一併取代);不存在 → 404。

    budget 的 `exhausted_until` 不由 body 設定:保留既有值(編輯不清掉冷卻;清除請用 clear-cooldown);
    body 的 `budget` 為 null 時刪除 budgets 列(冷卻一併消失)。
    變更 provider 時依新 provider 的 cardinality 在同一交易內檢查(決策 22)。
    """
    if body.id is not None and body.id != dep_id:
        raise _invalid("body.id", "不可變更成員 id(必須與路徑相同)")
    with _db(request) as conn:
        with transaction(conn):
            old = _load_dep(conn, dep_id)
            until = old.budget.exhausted_until if old.budget is not None else None
            dep = body.to_deployment(dep_id, until)
            if dep.provider != old.provider:
                _check_cardinality(conn, request.app.state.registry, dep)
            repo.save_deployment(conn, dep)
        return _dep_out(_load_dep(conn, dep_id))


@router.delete("/deployments/{dep_id}", status_code=204)
async def delete_deployment(request: Request, dep_id: str) -> Response:
    """刪除成員;tags、budget、route_targets 串聯刪除,帳本列保留(孤兒列)。回 204。"""
    with _db(request) as conn:
        if not repo.delete_deployment(conn, dep_id):
            raise _not_found(f"成員 {dep_id}")
    return Response(status_code=204)


@router.post("/deployments/{dep_id}/clear-cooldown")
async def clear_cooldown(request: Request, dep_id: str) -> dict[str, Any]:
    """清除 `exhausted_until`(spec 5.3.3,`QuotaService.clear`);回成員。"""
    with _db(request) as conn:
        dep = _load_dep(conn, dep_id)
        QuotaService(conn, _clock(request)).clear(dep)
        return _dep_out(_load_dep(conn, dep_id))


@router.post("/deployments/{dep_id}/test")
async def test_deployment(request: Request, dep_id: str) -> dict[str, Any]:
    """對該成員送一次最小測試呼叫(messages = `[{"role":"user","content":"ping"}]`、
    `max_tokens=16`、不指定 effort),不經路由與 fallback、不看 enabled 與冷卻。

    實作選擇:Router 只有 `local/` 直呼能指定單一成員,cli/api 成員無法經它呼叫;因此直接取
    `registry.get(dep)` 呼叫 adapter,但比照 Router 的直呼規則記錄:呼叫前以 Estimator 預估、
    帳本一次寫入(`route_name`、`slot` 為 NULL,payload 依全域設定)、`extra_secrets` 為當次解析的
    憑證值、cli/api 成員遇 `quota_exhausted`/`rate_limited` 以 `QuotaService.mark` 設冷卻。

    回應:`{deployment, status, ok, latency_ms, tokens_in, tokens_out, cost_usd, text, error,
    ledger_id}`;`text` 為回應文字前 200 字元(已遮罩)、`error` 已遮罩。模組不可用時 status 為
    `adapter_unavailable`,不呼叫、不寫帳本(`ledger_id` null)。
    """
    clock = _clock(request)
    registry = request.app.state.registry
    with _db(request) as conn:
        dep = _load_dep(conn, dep_id)
        try:
            adapter = registry.get(dep)
        except AdapterUnavailableError as exc:
            return {"deployment": dep.id, "status": "adapter_unavailable", "ok": False,
                    "latency_ms": None, "tokens_in": None, "tokens_out": None,
                    "cost_usd": None, "text": None, "error": exc.reason, "ledger_id": None}
        req = ChatRequest(model=dep.model_name,
                          messages=[{"role": "user", "content": TEST_PROMPT}],
                          max_tokens=TEST_MAX_TOKENS)
        est = Estimator(conn).estimate(dep, req, None)
        secrets: tuple[str, ...] = ()
        if dep.credential_ref:
            try:
                secrets = (credentials.resolve(dep.credential_ref),)
            except credentials.CredentialError:
                secrets = ()
        ts = clock.now()
        start = time.perf_counter()
        try:
            result = await adapter.call(dep, req, None)
            if not isinstance(result, CallResult):
                raise TypeError(f"adapter 回傳 {type(result).__name__},不是 CallResult")
        except Exception as exc:  # noqa: BLE001 — 與 Router 相同:例外視為 error
            result = CallResult(ok=False, text="", tokens_in=None, tokens_out=None,
                                cost_usd=None, status="error", error=f"{type(exc).__name__}: {exc}")
        latency_ms = max(0, int((time.perf_counter() - start) * 1000))
        status = result.status if result.status in CALL_STATUSES else "error"
        if status in ("quota_exhausted", "rate_limited") and dep.kind in CLOUD_KINDS:
            QuotaService(conn, clock).mark(dep, status)
        ledger_id = LedgerWriter(conn, _REDACTOR).record(Attempt(
            ts=ts, status=status, route_name=None, deployment_id=dep.id, effort=None,
            tokens_in=result.tokens_in, tokens_out=result.tokens_out, cost_usd=result.cost_usd,
            slot=None, fallback_depth=0, latency_ms=latency_ms,
            est_tokens_in=est.tokens_in, est_tokens_out_p50=est.out_p50,
            est_tokens_out_p90=est.out_p90, est_cost_usd_p90=est.cost_usd_p90,
            error=None if status == "ok" else (result.error or f"status {result.status}"),
            request_text=json.dumps(req.messages, ensure_ascii=False),
            response_text=result.text or None, extra_secrets=secrets,
            route_record_context=None,
        ))
    text = None
    if result.text:
        text, _ = _REDACTOR.redact(result.text[:TEST_TEXT_PREVIEW], secrets)
    error = None
    if status != "ok":
        error, _ = _REDACTOR.redact(result.error or f"status {result.status}", secrets)
    return {"deployment": dep.id, "status": status, "ok": status == "ok",
            "latency_ms": latency_ms, "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out, "cost_usd": result.cost_usd,
            "text": text, "error": error, "ledger_id": ledger_id}


# ================================================================ /api/groups


class SelectorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    all: list[Tag] = Field(default_factory=list)
    any: list[Tag] = Field(default_factory=list)
    none: list[Tag] = Field(default_factory=list)


class GroupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200, pattern=_ID_PATTERN)
    selector: SelectorIn


class GroupUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selector: SelectorIn


def _member_brief(d: Deployment) -> dict[str, Any]:
    return {"id": d.id, "model_name": d.model_name, "kind": d.kind, "provider": d.provider,
            "enabled": d.enabled}


def _group_out(g: repo.Group, deps: list[Deployment]) -> dict[str, Any]:
    sel = {k: list(g.selector.get(k) or []) for k in ("all", "any", "none")}
    return {"name": g.name, "selector": sel,
            "members": [_member_brief(d) for d in members_of({"tags": sel}, deps)]}


def _load_group(conn: sqlite3.Connection, name: str) -> repo.Group:
    g = repo.get_group(conn, name)
    if g is None:
        raise _not_found(f"群組 {name}")
    return g


@router.get("/groups")
async def list_groups(request: Request) -> dict[str, Any]:
    """`{"groups": [群組, …]}`(依 name 排序;格式同單一群組)。"""
    with _db(request) as conn:
        deps = repo.list_deployments(conn)
        return {"groups": [_group_out(g, deps) for g in repo.list_groups(conn)]}


@router.post("/groups", status_code=201)
async def create_group(request: Request, body: GroupIn) -> dict[str, Any]:
    """建立群組;重複 name → 409 `conflict`。"""
    with _db(request) as conn:
        with transaction(conn):
            if repo.get_group(conn, body.name) is not None:
                raise ApiError(409, "conflict", f"群組已存在:{body.name}")
            repo.save_group(conn, repo.Group(body.name, body.selector.model_dump()))
        return _group_out(_load_group(conn, body.name), repo.list_deployments(conn))


@router.get("/groups/{name}")
async def get_group(request: Request, name: str) -> dict[str, Any]:
    """`{name, selector: {all, any, none}, members: [{id, model_name, kind, provider, enabled}]}`;
    `members` 為目前符合選擇器的成員預覽(含停用者,依 id 排序)。"""
    with _db(request) as conn:
        return _group_out(_load_group(conn, name), repo.list_deployments(conn))


@router.put("/groups/{name}")
async def update_group(request: Request, name: str, body: GroupUpdate) -> dict[str, Any]:
    """取代選擇器;不存在 → 404。"""
    with _db(request) as conn:
        with transaction(conn):
            _load_group(conn, name)
            repo.save_group(conn, repo.Group(name, body.selector.model_dump()))
        return _group_out(_load_group(conn, name), repo.list_deployments(conn))


@router.delete("/groups/{name}", status_code=204)
async def delete_group(request: Request, name: str) -> Response:
    """刪除群組;仍被 route 參照 → 409 `conflict`(message 列出 route 名)。回 204。"""
    with _db(request) as conn:
        with transaction(conn):
            _load_group(conn, name)
            refs = [r.name for r in repo.list_routes(conn) if r.target.get("group") == name]
            if refs:
                raise ApiError(409, "conflict", f"群組 {name} 仍被 route 參照:{', '.join(refs)}")
            repo.delete_group(conn, name)
    return Response(status_code=204)


# ================================================================ /api/routes


def _normalize_target(v: Any) -> dict[str, Any]:
    if not isinstance(v, Mapping):
        raise ValueError("必須是 {group} 或 {tags: {all, any, none}}")
    keys = set(v)
    if keys == {"group"}:
        g = v["group"]
        if not isinstance(g, str) or not g:
            raise ValueError("group 必須是非空字串")
        return {"group": g}
    if keys == {"tags"}:
        try:
            sel = SelectorIn.model_validate(v["tags"])
        except ValidationError as exc:
            raise ValueError(
                "tags 必須是 {all, any, none} 字串清單(" + _format_validation(
                    [{**e, "loc": ("tags", *e["loc"])} for e in exc.errors()]) + ")") from None
        return {"tags": sel.model_dump()}
    raise ValueError("必須是 {group} 或 {tags: {all, any, none}} 其中之一")


class RouteIn(BaseModel):
    """POST 與 PUT 共用(PUT 為整筆取代,`name` 可省略,給了就必須與路徑相同)。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200, pattern=_ROUTE_PATTERN)
    target: dict[str, Any]
    mode: Literal["chat", "task"] = "chat"
    tags: list[Tag] = Field(default_factory=list)
    record_context: StrictBool | None = None      # null = 沿用全域(spec 2.4.2)
    budget_precheck: StrictBool = False

    @field_validator("target", mode="before")
    @classmethod
    def _check_target(cls, v: Any) -> dict[str, Any]:
        return _normalize_target(v)

    def to_route(self, name: str) -> repo.Route:
        return repo.Route(name=name, target=self.target, mode=self.mode, tags=tuple(self.tags),
                          record_context=self.record_context,
                          budget_precheck=self.budget_precheck)


class OrderEntryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment: str = Field(min_length=1)
    effort: EffortLit | None = None
    weight: WeightLit = "medium"


class OrderIn(BaseModel):
    """spec 9.3:外層清單順序 = slot(1 起),內層順序 = position(0 起)。"""

    model_config = ConfigDict(extra="forbid")

    slots: list[Annotated[list[OrderEntryIn], Field(min_length=1)]]

    @field_validator("slots")
    @classmethod
    def _no_duplicates(cls, v: list[list[OrderEntryIn]]) -> list[list[OrderEntryIn]]:
        seen: set[str] = set()
        dup: list[str] = []
        for slot in v:
            for e in slot:
                if e.deployment in seen:
                    dup.append(e.deployment)
                seen.add(e.deployment)
        if dup:
            raise ValueError(f"同一成員出現多次:{', '.join(sorted(set(dup)))}")
        return v


def _order_out(targets: list[repo.RouteTarget]) -> list[list[dict[str, Any]]]:
    slots: dict[int, list[dict[str, Any]]] = {}
    for t in targets:   # 已依 (slot, position, id) 排序
        slots.setdefault(t.slot, []).append(
            {"deployment": t.deployment_id, "effort": t.effort, "weight": t.weight})
    return [slots[k] for k in sorted(slots)]


def _route_out(conn: sqlite3.Connection, r: repo.Route) -> dict[str, Any]:
    """`{name, target, mode, tags[], record_context, budget_precheck, order}`;`order` 為手動排序
    (同 9.3 的 slots 形狀,每筆 `{deployment, effort, weight}`),未手動排序時為 `[]`。"""
    return {"name": r.name, "target": r.target, "mode": r.mode, "tags": list(r.tags),
            "record_context": r.record_context, "budget_precheck": r.budget_precheck,
            "order": _order_out(repo.list_route_targets(conn, r.name))}


def _load_route(conn: sqlite3.Connection, name: str) -> repo.Route:
    r = repo.get_route(conn, name)
    if r is None:
        raise _not_found(f"route {name}")
    return r


def _check_group_exists(conn: sqlite3.Connection, target: Mapping[str, Any]) -> None:
    if "group" in target and repo.get_group(conn, target["group"]) is None:
        raise _invalid("body.target.group", f"群組 {target['group']} 不存在")


@router.get("/routes")
async def list_routes(request: Request) -> dict[str, Any]:
    """`{"routes": [route, …]}`(依 name 排序;格式同單一 route)。"""
    with _db(request) as conn:
        return {"routes": [_route_out(conn, r) for r in repo.list_routes(conn)]}


@router.post("/routes", status_code=201)
async def create_route(request: Request, body: RouteIn) -> dict[str, Any]:
    """建立 route;`{group}` 目標的群組必須存在(否則 422);重複 name → 409 `conflict`。"""
    if body.name is None:
        raise _invalid("body.name", "Field required")
    with _db(request) as conn:
        with transaction(conn):
            if repo.get_route(conn, body.name) is not None:
                raise ApiError(409, "conflict", f"route 已存在:{body.name}")
            _check_group_exists(conn, body.target)
            repo.save_route(conn, body.to_route(body.name))
        return _route_out(conn, _load_route(conn, body.name))


@router.get("/routes/{name}")
async def get_route(request: Request, name: str) -> dict[str, Any]:
    with _db(request) as conn:
        return _route_out(conn, _load_route(conn, name))


@router.put("/routes/{name}")
async def update_route(request: Request, name: str, body: RouteIn) -> dict[str, Any]:
    """整筆取代 route 設定(未給的欄位回到預設);既有手動排序保留(目標變更後不再屬於目標的成員,
    由 Router 依目標過濾,不在此刪除)。"""
    if body.name is not None and body.name != name:
        raise _invalid("body.name", "不可變更 route 名稱(必須與路徑相同)")
    with _db(request) as conn:
        with transaction(conn):
            _load_route(conn, name)
            _check_group_exists(conn, body.target)
            repo.save_route(conn, body.to_route(name))
        return _route_out(conn, _load_route(conn, name))


@router.delete("/routes/{name}", status_code=204)
async def delete_route(request: Request, name: str) -> Response:
    """刪除 route 與其 route_targets。回 204。"""
    with _db(request) as conn:
        if not repo.delete_route(conn, name):
            raise _not_found(f"route {name}")
    return Response(status_code=204)


@router.put("/routes/{name}/order")
async def put_route_order(request: Request, name: str, body: OrderIn) -> dict[str, Any]:
    """儲存拖曳結果(spec 9.3)。成員不存在或不屬 route 目標 → 422 `member_not_in_target`
    (message 列出成員 id);以 `repo.replace_route_targets` 單一交易取代全部 route_targets
    (9.3.2;驗證失敗時舊資料不變)。回 route(含新的 `order`)。`slots` 為空清單等同還原預設。"""
    with _db(request) as conn:
        with transaction(conn):
            route = _load_route(conn, name)
            members = {d.id for d in members_of(route.target, repo.list_deployments(conn),
                                                repo.list_groups(conn))}
            outside = [e.deployment for slot in body.slots for e in slot
                       if e.deployment not in members]
            if outside:
                raise ApiError(422, "member_not_in_target",
                               f"成員不屬於 route {name} 的目標:{', '.join(outside)}")
            repo.replace_route_targets(conn, name, [
                repo.RouteTarget(deployment_id=e.deployment, slot=i + 1, position=j,
                                 effort=e.effort, weight=e.weight)
                for i, slot in enumerate(body.slots) for j, e in enumerate(slot)
            ])
        return _route_out(conn, route)


@router.delete("/routes/{name}/order", status_code=204)
async def delete_route_order(request: Request, name: str) -> Response:
    """還原預設順序(spec 4.3.4):刪除該 route 的全部 route_targets。回 204。"""
    with _db(request) as conn:
        with transaction(conn):
            _load_route(conn, name)
            repo.replace_route_targets(conn, name, [])
    return Response(status_code=204)


# ================================================================ dry-run、estimate


class ChatBody(BaseModel):
    """範例請求(OpenAI 形狀);未列出的參數原樣放進 `ChatRequest.extra`。`stream` 不影響 dry-run。"""

    model_config = ConfigDict(extra="allow")

    model: str | None = Field(default=None, min_length=1)
    messages: list[dict[str, Any]] = Field(min_length=1)
    max_tokens: StrictInt | None = Field(default=None, ge=1)
    reasoning_effort: str | None = None
    tools: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    stream: bool = False

    @field_validator("reasoning_effort")
    @classmethod
    def _effort(cls, v: str | None) -> str | None:
        try:
            return normalize_effort(v)
        except InvalidEffort:
            raise ValueError("必須為 none、low、medium、high、max(或 minimal)") from None

    def to_request(self, model: str) -> ChatRequest:
        return ChatRequest(model=model, messages=self.messages, max_tokens=self.max_tokens,
                           reasoning_effort=self.reasoning_effort, tools=self.tools,
                           metadata=dict(self.metadata), stream=False,
                           extra=dict(self.model_extra or {}))


class EstimateBody(ChatBody):
    model: str = Field(min_length=1)


def _plan_out(plan: Plan) -> dict[str, Any]:
    """Plan → JSON:

    `{route_name, slots: [{number, entries: [{deployment, kind, weight, effort, position, unsorted,
    health, estimate}]}], excluded: [{deployment, reason, estimate}], unsorted: [id…], first}`。
    `estimate` 為 `{tokens_in, out_p50, out_p90, cost_usd_p90}` 或 null;slot 內 entries 依本次 Rng
    抽出的順序;`health` 對 cli/api 為 null、local 為 `healthy`/`health_unknown`;`first` 為本次
    抽出的第一順位(沒有候選時為 null)。`local/` 直呼時 `route_name` 與 slot `number` 為 null。
    """
    return {
        "route_name": plan.route_name,
        "slots": [
            {"number": s.number,
             "entries": [{"deployment": e.deployment, "kind": e.kind, "weight": e.weight,
                          "effort": e.effort, "position": e.position, "unsorted": e.unsorted,
                          "health": e.health, "estimate": _estimate_out(e.estimate)}
                         for e in s.entries]}
            for s in plan.slots
        ],
        "excluded": [{"deployment": x.deployment, "reason": x.reason,
                      "estimate": _estimate_out(x.estimate)} for x in plan.excluded],
        "unsorted": list(plan.unsorted),
        "first": plan.first,
    }


async def _dry_run(request: Request, model: str, req: ChatRequest) -> Plan:
    try:
        return await request.app.state.router.dry_run(model, req)
    except RouteNotFound:
        raise _not_found(f" route 或成員:{model}") from None
    except InvalidEffort:
        raise _invalid("body.reasoning_effort", "必須為 none、low、medium、high、max") from None


@router.post("/routes/{name}/dry-run")
async def dry_run_route(request: Request, name: str, body: ChatBody) -> dict[str, Any]:
    """以範例請求模擬路由(不呼叫上游、不寫帳本);route 不存在 → 404。body 的 `model` 忽略。
    回應為 Plan JSON(見 `_plan_out`)。"""
    with _db(request) as conn:
        _load_route(conn, name)
    plan = await _dry_run(request, f"route:{name}", body.to_request(f"route:{name}"))
    return _plan_out(plan)


@router.post("/estimate")
async def estimate(request: Request, body: EstimateBody) -> dict[str, Any]:
    """以請求 body(`model` 依 4.1 解析:route、`route:`、`model_name`、`local/<id>`)回傳各候選的
    預估,不呼叫上游(spec 9.2.1)。無法解析 → 404 `not_found`。

    回應:`{route_name, candidates: [{deployment, kind, slot, estimate}], excluded: [{deployment,
    reason, estimate}]}`;candidates 依 slot 與本次抽選順序,`skipped_precheck` 的成員在 excluded
    且帶 estimate。
    """
    plan = await _dry_run(request, body.model, body.to_request(body.model))
    return {
        "route_name": plan.route_name,
        "candidates": [{"deployment": e.deployment, "kind": e.kind, "slot": s.number,
                        "estimate": _estimate_out(e.estimate)}
                       for s in plan.slots for e in s.entries],
        "excluded": [{"deployment": x.deployment, "reason": x.reason,
                      "estimate": _estimate_out(x.estimate)} for x in plan.excluded],
    }


# ================================================================ /api/ledger


def _parse_time(value: str | None, path: str) -> datetime | None:
    """ISO 8601(`2026-10-30T12:00:00Z`、`+00:00`)或日期(`2026-10-30` = 當日 00:00);
    沒有時區時視為 UTC。"""
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise _invalid(path, "必須是 ISO 8601 時間(例:2026-10-30T12:00:00Z)") from None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _ledger_out(r: repo.LedgerRow) -> dict[str, Any]:
    d = dataclasses.asdict(r)
    d["ts"] = to_iso(r.ts)
    return {"id": d.pop("id"), **d}


StatusLit = Literal["ok", "error", "quota_exhausted", "rate_limited", "skipped_precheck"]
assert set(StatusLit.__args__) == LEDGER_STATUSES  # type: ignore[attr-defined]


@router.get("/ledger")
async def list_ledger(
    request: Request,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    deployment: str | None = None,
    route: str | None = None,
    status: StatusLit | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=LEDGER_PAGE_SIZE_MAX)] = LEDGER_PAGE_SIZE,
) -> dict[str, Any]:
    """帳本查詢(spec 9.2)。

    參數:`from`(含)、`to`(不含)為 ISO 8601 時間或日期(無時區視為 UTC);`deployment`、`route`、
    `status` 為等值篩選;`page` 自 1 起;`page_size` 預設 50、上限 500(本筆另加的可選參數)。

    分頁格式:`{items: [帳本列…], page, page_size, total, pages}`;`items` 依 `ts DESC, id DESC`
    (新到舊);`total` 為符合條件的總列數,`pages = ceil(total / page_size)`(total 為 0 時 0);
    超過最後一頁時 `items` 為空。帳本列欄位同 `usage_ledger`(spec 2.3),`ts` 為 ISO 字串。
    """
    since = _parse_time(from_, "query.from")
    until = _parse_time(to, "query.to")
    with _db(request) as conn:
        rows = repo.list_ledger(conn, route_name=route, deployment_id=deployment, status=status,
                                since=since, until=until, limit=page_size,
                                offset=(page - 1) * page_size)
        where: list[str] = []
        args: list[Any] = []
        for col, val in (("route_name", route), ("deployment_id", deployment), ("status", status)):
            if val is not None:
                where.append(f"{col} = ?")
                args.append(val)
        if since is not None:
            where.append("ts >= ?")
            args.append(to_iso(since))
        if until is not None:
            where.append("ts < ?")
            args.append(to_iso(until))
        sql = "SELECT COUNT(*) FROM usage_ledger" + (" WHERE " + " AND ".join(where) if where else "")
        total = int(conn.execute(sql, args).fetchone()[0])
    return {"items": [_ledger_out(r) for r in rows], "page": page, "page_size": page_size,
            "total": total, "pages": math.ceil(total / page_size)}


@router.get("/ledger/{ledger_id}/payload")
async def get_payload(request: Request, ledger_id: int) -> dict[str, Any]:
    """該次嘗試的請求與回應內容(spec 9.2.1;決策 17,依 `ledger.payload_status`)。

    200:`{ledger_id, ts, request_json, response_text, bytes, truncated, redacted}`(內容已於寫入時遮罩);
    已過保存期限 → 410 `payload_expired`;從未記錄或帳本列不存在 → 404 `not_found`。
    """
    with _db(request) as conn:
        try:
            st = payload_status(conn, ledger_id, _clock(request).now())
        except LedgerNotFound:
            raise _not_found(f"帳本列 {ledger_id}") from None
        if st == "expired":
            raise ApiError(410, "payload_expired", f"帳本列 {ledger_id} 的內容已過保存期限")
        p = repo.get_payload(conn, ledger_id) if st == "ok" else None
        if p is None:
            raise _not_found(f"帳本列 {ledger_id} 的內容(未記錄)")
    return {"ledger_id": p.ledger_id, "ts": to_iso(p.ts), "request_json": p.request_json,
            "response_text": p.response_text, "bytes": p.bytes, "truncated": p.truncated,
            "redacted": p.redacted}


# ================================================================ /api/settings/context


class ContextSettingsIn(BaseModel):
    """部分更新:未給或為 null 的鍵不變。`context_retention_days` ≥ 0(0 = 不保存內容,spec 2.4.5);
    `context_max_bytes` ≥ 1024(`CONTEXT_MAX_BYTES_MIN`)。型別嚴格(不接受字串數字、1/0 當布林)。"""

    model_config = ConfigDict(extra="forbid")

    record_context_default: StrictBool | None = None
    context_retention_days: StrictInt | None = Field(default=None, ge=0)
    context_max_bytes: StrictInt | None = Field(default=None, ge=CONTEXT_MAX_BYTES_MIN)


def _context_out(conn: sqlite3.Connection) -> dict[str, Any]:
    s = repo.get_context_settings(conn)
    return {"record_context_default": s.record_default, "context_retention_days": s.retention_days,
            "context_max_bytes": s.max_bytes}


@router.get("/settings/context")
async def get_context_settings(request: Request) -> dict[str, Any]:
    """`{record_context_default: bool, context_retention_days: int, context_max_bytes: int}`;
    資料庫缺鍵時為常數預設值(決策 24),讀取不寫入資料列。"""
    with _db(request) as conn:
        return _context_out(conn)


@router.put("/settings/context")
async def put_context_settings(request: Request, body: ContextSettingsIn) -> dict[str, Any]:
    """部分更新(同一交易);回更新後的三個值。"""
    with _db(request) as conn:
        with transaction(conn):
            for key, value in body.model_dump(exclude_none=True).items():
                repo.set_setting(conn, key, value)
        return _context_out(conn)


# ================================================================ /api/dashboard

_DEFAULT_PERIOD = Budget(type="none", limit_amount=None, window="monthly", reset_anchor=1)


@router.get("/dashboard")
async def dashboard(request: Request) -> dict[str, Any]:
    """各成員額度總覽(spec 9.2、5.1、5.4.6;決策 6)。

    回應 `{generated_at, deployments: [...]}`,每個成員(依 id,只列現存成員;孤兒帳本列不計):
    `id`、`model_name`、`kind`、`provider`、`enabled`、`budget_type`、`limit_amount`、`window`、
    `period_start`、`next_reset`、`period_spend`、`remaining`、`unlimited`、`ratio`、`cooling`、
    `cooling_until`、`requests_24h`、`est_days_left`。

    - 本期花費 = 帳本該成員 `ts` ∈ [period_start, next_reset) 的 `cost_usd` 加總(不分 status,
      NULL 不計),與 `QuotaService.remaining()` 相同;window `none` 為累計(period_start、next_reset
      為 null)。沒有 budgets 列的成員以日曆月(1 日起)計本期花費,`window` 為 null。
    - 無上限(無 budgets 列、type `none`、`limit_amount` NULL):`remaining` null、`unlimited` true、
      `ratio` 1。否則 `remaining = limit − 本期花費`(可為負)、`ratio` 夾在 0–1、上限 0 時 0。
    - `cooling_until`:`exhausted_until` 尚未到期時的值,否則 null;`cooling` 為其布林。
    - `requests_24h`:近 24 小時(`ts ≥ now − 24h`)帳本列數,不含 `skipped_precheck`(沒有送出請求)。
    - `est_days_left`(5.4.6):`max(remaining, 0) ÷ (近 7 天實際花費 ÷ 7)`;無上限或近 7 天無花費時 null。

    效能(NFR-2):查詢數與成員數無關——成員與 budgets 一次 JOIN、每種相異期間一次 GROUP BY 加總、
    近 24 小時請求數與近 7 天花費合併一次 GROUP BY。repo 沒有彙總查詢,這裡以參數化 SQL 撰寫(唯讀)。
    """
    clock = _clock(request)
    now = clock.now().astimezone(timezone.utc)
    with _db(request) as conn:
        quota = QuotaService(conn, clock)
        rows = conn.execute(
            "SELECT d.id, d.model_name, d.kind, d.provider, d.enabled, b.type AS b_type, "
            "b.limit_amount, b.window, b.reset_anchor, b.exhausted_until, "
            "b.deployment_id IS NOT NULL AS has_budget "
            "FROM deployments d LEFT JOIN budgets b ON b.deployment_id = d.id ORDER BY d.id"
        ).fetchall()

        periods: dict[str, tuple[str | None, str | None]] = {}
        for r in rows:
            if r["has_budget"]:
                b = Budget(type=r["b_type"], limit_amount=r["limit_amount"], window=r["window"],
                           reset_anchor=r["reset_anchor"])
            else:
                b = _DEFAULT_PERIOD
            if b.window == "none":
                periods[r["id"]] = (None, None)
            else:
                start = quota.period_start(b, now)
                periods[r["id"]] = (_iso(start), _iso(quota.next_reset(b, now)))

        spent: dict[tuple[str | None, str | None], dict[str, float]] = {}
        for key in set(periods.values()):
            start, end = key
            sql = "SELECT deployment_id, COALESCE(SUM(cost_usd), 0) FROM usage_ledger"
            args: list[Any] = []
            if start is not None:
                sql += " WHERE ts >= ? AND ts < ?"
                args = [start, end]
            sql += " GROUP BY deployment_id"
            spent[key] = {d: float(s) for d, s in conn.execute(sql, args)}

        recent = {
            d: (int(n24), float(s7))
            for d, n24, s7 in conn.execute(
                "SELECT deployment_id, "
                "SUM(CASE WHEN ts >= ? AND status != 'skipped_precheck' THEN 1 ELSE 0 END), "
                "COALESCE(SUM(cost_usd), 0) "
                "FROM usage_ledger WHERE ts >= ? GROUP BY deployment_id",
                (to_iso(now - timedelta(hours=24)), to_iso(now - timedelta(days=7))),
            )
        }

    out = []
    for r in rows:
        key = periods[r["id"]]
        spend = spent[key].get(r["id"], 0.0)
        unlimited = (not r["has_budget"]) or r["b_type"] == "none" or r["limit_amount"] is None
        if unlimited:
            remaining, ratio = None, 1.0
        else:
            limit = float(r["limit_amount"])
            remaining = limit - spend
            ratio = 0.0 if limit <= 0 else min(1.0, max(0.0, remaining / limit))
        until = r["exhausted_until"]
        cooling_until = until if until is not None and until > to_iso(now) else None
        n24, spend7 = recent.get(r["id"], (0, 0.0))
        days_left = None
        if remaining is not None and spend7 > 0:
            days_left = max(remaining, 0.0) / (spend7 / 7)
        out.append({
            "id": r["id"], "model_name": r["model_name"], "kind": r["kind"],
            "provider": r["provider"], "enabled": bool(r["enabled"]),
            "budget_type": r["b_type"], "limit_amount": r["limit_amount"],
            "window": r["window"] if r["has_budget"] else None,
            "period_start": key[0], "next_reset": key[1], "period_spend": spend,
            "remaining": remaining, "unlimited": unlimited, "ratio": ratio,
            "cooling": cooling_until is not None, "cooling_until": cooling_until,
            "requests_24h": n24, "est_days_left": days_left,
        })
    return {"generated_at": to_iso(now), "deployments": out}


# ================================================================ /api/adapters


def _module_counts(registry: Any, modules: list[ModuleInfo],
                   deps: list[tuple[str, str]]) -> dict[str, int]:
    """每個模組的現有成員數:可用 provider 依 `registry.module_for`(含後備模組),
    無法解析時以模組宣告(或推得)的 providers 比對。"""
    counts = {m.name: 0 for m in modules}
    for kind, provider in deps:
        probe = Deployment(id="", model_name="", kind=kind, provider=provider, model="")
        try:
            name = registry.module_for(probe).name
        except AdapterUnavailableError:
            name = next((m.name for m in modules if provider in m.providers), None)
        if name in counts:
            counts[name] += 1
    return counts


@router.get("/adapters")
async def list_adapters(request: Request) -> dict[str, Any]:
    """已發現的 adapter 模組(spec 9.2.2;design D13 的形狀):

    `{"modules": [{module, builtin, providers[], kinds[], access[], cardinality, fallback_for_kinds[],
    existing_members, available, unavailable_reason, models[], models_error}]}`

    - 依 `registry.modules()` 的順序(內建模組在前,含載入失敗者)。
    - `access` 為清單(manifest 允許多值,例如 litellm 的 `["api", "local"]`)。
    - 載入失敗的模組:`available` false、`unavailable_reason` 為原因、`kinds`/`access`/`models` 為空、
      `cardinality` null;`providers` 為推得值(決策 14)。
    - `existing_members`:目前由此模組處理的成員數(UI 依 `cardinality` 判斷 single 是否已滿)。
    - `models`:`available_models()`——`models.yaml` 合併 `list_models()`(同 id 以 yaml 為準);
      查詢失敗時只回 yaml,原因放 `models_error`。litellm 模組的 local 端點取自環境變數
      `SPILLWAY_LOCAL_API_BASE`(決策 25,由模組自行讀取)。各模組的查詢並行執行。
    """
    registry = request.app.state.registry
    ctx = AdapterContext(credentials=credentials.resolve, clock=_clock(request))
    with _db(request) as conn:
        deps = [(r[0], r[1]) for r in conn.execute("SELECT kind, provider FROM deployments")]
    modules = registry.modules()
    counts = _module_counts(registry, modules, deps)
    listings = await asyncio.gather(*(
        available_models(m, ctx) for m in modules if m.available))
    by_name = dict(zip((m.name for m in modules if m.available), listings))
    out = []
    for m in modules:
        man = m.manifest if m.available else None
        listing = by_name.get(m.name)
        out.append({
            "module": m.name,
            "builtin": m.builtin,
            "providers": list(m.providers),
            "kinds": list(man.kinds) if man else [],
            "access": list(man.access) if man else [],
            "cardinality": man.cardinality if man else None,
            "fallback_for_kinds": list(man.fallback_for_kinds) if man else [],
            "existing_members": counts.get(m.name, 0),
            "available": m.available,
            "unavailable_reason": None if m.available else m.reason,
            "models": [x.to_dict() for x in listing.models] if listing else [],
            "models_error": listing.error if listing else None,
        })
    return {"modules": out}

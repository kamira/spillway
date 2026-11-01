"""`/api/deployments`(含 clear-cooldown、test-call;CHG-20261101-02 決策 9)。"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import sqlite3
import time
from datetime import datetime
from typing import Any

from fastapi import Request, Response

from spillway import credentials
from spillway.adapters.base import AdapterUnavailableError
from spillway.api import ApiError, redact_echo
from spillway.api.deps import request_conn
from spillway.domain import (
    CALL_STATUSES,
    CLOUD_KINDS,
    COOLING_STATUSES,
    Budget,
    CallResult,
    ChatRequest,
    Deployment,
)
from spillway.estimate import Estimator
from spillway.ledger import Attempt, LedgerWriter
from spillway.quota import QuotaService
from spillway.store import repo
from spillway.store.db import transaction

from .common import _REDACTOR, _clock, _invalid, _iso, _not_found, router
from .models import DeploymentIn


TEST_PROMPT = "ping"
TEST_MAX_TOKENS = 16
TEST_TEXT_PREVIEW = 200          # 測試呼叫回傳的文字預覽上限(字元)

_log = logging.getLogger("spillway.api.admin")


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


def _cooling_only(until: datetime | None) -> Budget:
    """只為記錄冷卻而存在的 budgets 列(與 `QuotaService.mark` 自動建立的列同形)。"""
    return Budget(type="none", limit_amount=None, window="none", exhausted_until=until)



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
    others = repo.provider_member_ids(conn, dep.provider, exclude_id=dep.id)
    if others:
        raise ApiError(
            409, "provider_single",
            redact_echo(f"provider {dep.provider} 的模組僅允許 1 個成員,已有 1 個:{others[0]}"))


# ================================================================ /api/deployments


@router.get("/deployments")
async def list_deployments(request: Request) -> dict[str, Any]:
    """`{"deployments": [成員, …]}`(依 id 排序;成員格式同 `GET /api/deployments/{id}`)。"""
    with request_conn(request) as conn:
        return {"deployments": [_dep_out(d) for d in repo.list_deployments(conn)]}


@router.post("/deployments", status_code=201)
async def create_deployment(request: Request, body: DeploymentIn) -> dict[str, Any]:
    """建立成員(含 tags、budget),回 201 與成員。重複 id → 409 `conflict`;
    `single` 模組的 provider 已有成員 → 409 `provider_single`(同一交易內檢查)。"""
    if body.id is None:
        raise _invalid("body.id", "Field required")
    dep = body.to_deployment(body.id, None)
    with request_conn(request) as conn:
        with transaction(conn):
            if repo.deployment_exists(conn, dep.id):
                raise ApiError(409, "conflict", redact_echo(f"成員 id 已存在:{dep.id}"))
            _check_cardinality(conn, request.app.state.registry, dep)
            repo.save_deployment(conn, dep)
        return _dep_out(_load_dep(conn, dep.id))


@router.get("/deployments/{dep_id}")
async def get_deployment(request: Request, dep_id: str) -> dict[str, Any]:
    """單一成員:`id`、`model_name`、`kind`、`provider`、`model`、`params`、`credential_ref`、
    `capabilities`、`enabled`、`tags[]`、`budget`(`{type, limit_amount, window, reset_anchor,
    exhausted_until}` 或 null)。"""
    with request_conn(request) as conn:
        return _dep_out(_load_dep(conn, dep_id))


@router.put("/deployments/{dep_id}")
async def update_deployment(request: Request, dep_id: str, body: DeploymentIn) -> dict[str, Any]:
    """整筆取代(tags、budget 一併取代);不存在 → 404。

    budget 的 `exhausted_until` 不由 body 設定:保留既有值(編輯不清掉冷卻;清除請用 clear-cooldown);
    body 的 `budget` 為 null 時清除預算;冷卻尚未到期則保留(U10 (a)):budgets 列改為只記冷卻的
    `type=none`、`window=none`、`limit_amount` NULL 列,不刪除;沒有未到期的冷卻時刪除 budgets 列。
    變更 provider 或 kind 時依新 (provider, kind) 的 cardinality 在同一交易內檢查(決策 22)。
    成功後呼叫 `router.invalidate_health(dep_id)`(含 `enabled` 變更;CHG-20261101-02 決策 12)。
    """
    if body.id is not None and body.id != dep_id:
        raise _invalid("body.id", "不可變更成員 id(必須與路徑相同)")
    now = _clock(request).now()
    with request_conn(request) as conn:
        with transaction(conn):
            old = _load_dep(conn, dep_id)
            until = old.budget.exhausted_until if old.budget is not None else None
            dep = body.to_deployment(dep_id, until)
            if dep.budget is None and until is not None and until > now:
                dep = dataclasses.replace(dep, budget=_cooling_only(until))
            if dep.provider != old.provider or dep.kind != old.kind:
                _check_cardinality(conn, request.app.state.registry, dep)
            repo.save_deployment(conn, dep)
        # 交易已提交:健康結果可能依舊設定(api_base、enabled 等)而來,讓下一次經 route 的請求重新探測(決策 12)
        request.app.state.router.invalidate_health(dep_id)
        return _dep_out(_load_dep(conn, dep_id))


@router.delete("/deployments/{dep_id}", status_code=204)
async def delete_deployment(request: Request, dep_id: str) -> Response:
    """刪除成員;tags、budget、route_targets 串聯刪除,帳本列保留(孤兒列)。回 204。
    成功後呼叫 `router.invalidate_health(dep_id)`(CHG-20261101-02 決策 12)。"""
    with request_conn(request) as conn:
        if not repo.delete_deployment(conn, dep_id):
            raise _not_found(f"成員 {dep_id}")
    request.app.state.router.invalidate_health(dep_id)   # 決策 12:不留已刪除成員的健康結果
    return Response(status_code=204)


@router.post("/deployments/{dep_id}/clear-cooldown")
async def clear_cooldown(request: Request, dep_id: str) -> dict[str, Any]:
    """清除 `exhausted_until`(spec 5.3.3,`QuotaService.clear`);回成員。"""
    with request_conn(request) as conn:
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
    憑證值、cli/api 成員遇 `quota_exhausted`/`rate_limited` 以 `QuotaService.mark` 設冷卻
    (`mark` 失敗只記例外型別,帳本照寫);adapter 被取消時先寫一列 `status=error`、
    `error="cancelled"` 再重新丟出。

    回應:`{deployment, status, ok, latency_ms, tokens_in, tokens_out, cost_usd, text, error,
    ledger_id}`;`text` 為回應文字先遮罩、再取前 200 字元,`error` 已遮罩。模組不可用時 status 為
    `adapter_unavailable`(`error` 為經 Redactor 的原因),不呼叫、不寫帳本(`ledger_id` null)。
    """
    clock = _clock(request)
    registry = request.app.state.registry
    with request_conn(request) as conn:
        dep = _load_dep(conn, dep_id)
        try:
            adapter = registry.get(dep)
        except AdapterUnavailableError as exc:
            reason, _ = _REDACTOR.redact(str(exc.reason))
            return {"deployment": dep.id, "status": "adapter_unavailable", "ok": False,
                    "latency_ms": None, "tokens_in": None, "tokens_out": None,
                    "cost_usd": None, "text": None, "error": reason, "ledger_id": None}
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
        writer = LedgerWriter(conn, _REDACTOR)
        ts = clock.now()
        start = time.perf_counter()

        def elapsed_ms() -> int:
            return max(0, int((time.perf_counter() - start) * 1000))

        def record(res: CallResult, status: str, latency_ms: int) -> int:
            return writer.record(Attempt(
                ts=ts, status=status, route_name=None, deployment_id=dep.id, effort=None,
                tokens_in=res.tokens_in, tokens_out=res.tokens_out, cost_usd=res.cost_usd,
                slot=None, fallback_depth=0, latency_ms=latency_ms,
                est_tokens_in=est.tokens_in, est_tokens_out_p50=est.out_p50,
                est_tokens_out_p90=est.out_p90, est_cost_usd_p90=est.cost_usd_p90,
                error=None if status == "ok" else (res.error or f"status {res.status}"),
                request_text=json.dumps(req.messages, ensure_ascii=False),
                response_text=res.text or None, extra_secrets=secrets,
                route_record_context=None,
            ))

        try:
            result = await adapter.call(dep, req, None)
            if not isinstance(result, CallResult):
                raise TypeError(f"adapter 回傳 {type(result).__name__},不是 CallResult")
        except Exception as exc:  # noqa: BLE001 — 與 Router 相同:例外視為 error
            result = CallResult(ok=False, text="", tokens_in=None, tokens_out=None,
                                cost_usd=None, status="error", error=f"{type(exc).__name__}: {exc}")
        except asyncio.CancelledError:
            # 比照 routing/router.py:上游可能已收到請求,同步補一列帳本(不跨 await)後重新丟出
            record(CallResult(ok=False, text="", tokens_in=None, tokens_out=None, cost_usd=None,
                              status="error", error="cancelled"), "error", elapsed_ms())
            raise
        latency_ms = elapsed_ms()
        status = result.status if result.status in CALL_STATUSES else "error"
        if status in COOLING_STATUSES and dep.kind in CLOUD_KINDS:
            try:
                QuotaService(conn, clock).mark(dep, status)
            except Exception as exc:  # noqa: BLE001 — 冷卻設定失敗不可中斷帳本(決策 35;同 Router M18)
                _log.warning("QuotaService.mark 失敗(%s),本次測試呼叫未設冷卻",
                             type(exc).__name__)
        ledger_id = record(result, status, latency_ms)
    text = None
    if result.text:
        # 先遮罩再截斷:先截斷會讓橫跨第 200 字元的金鑰只剩前半段而漏遮(審議 M24)
        redacted, _ = _REDACTOR.redact(result.text, secrets)
        text = redacted[:TEST_TEXT_PREVIEW]
    error = None
    if status != "ok":
        error, _ = _REDACTOR.redact(result.error or f"status {result.status}", secrets)
    return {"deployment": dep.id, "status": status, "ok": status == "ok",
            "latency_ms": latency_ms, "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out, "cost_usd": result.cost_usd,
            "text": text, "error": error, "ledger_id": ledger_id}

"""OpenAI 相容端點 `/v1/*`(spec 9.1、9.2、1.1.3、4.1、4.5.3、5.4.5;CHG-20261030-02 T13)。

依賴 `request.app.state` 的 `router`(`spillway.routing.Router`)、`clock`、`conn_factory`(見 `app.py`)。

`POST /v1/chat/completions`
- body 以 Pydantic 驗證(OpenAI 相容欄位);其他欄位只轉交 `spillway.api.ALLOWED_CHAT_PARAMS`
  白名單內的 OpenAI 標準參數(放進 `ChatRequest.extra`),名單外的鍵靜默丟棄(U8)。
- `stream: true` → 400 `streaming_unsupported`(決策 7),不呼叫上游、不寫帳本。
- `reasoning_effort` 先以 `normalize_effort` 正規化(決策 29、40),不合法 → 422 `validation_error`。
- `RouteNotFound` → 404 `route_not_found`(4.1.4);回顯的 model 名先經 Redactor 遮罩。
- `RouteOutcome.result is None` → 503 `no_candidate`,message 先列每次嘗試「成員:狀態」,
  再列被篩成員「成員:原因」(決策 31);嘗試的錯誤內容已由 Router 遮罩,這裡只用成員 id 與狀態,
  不放錯誤原文。
- 其他例外(例如帳本寫入失敗,決策 35)→ 500 `internal_error`;日誌只記例外型別。
- 成功 → OpenAI `chat.completion` body(`model` 為請求的 model,`created` 取注入的 Clock)與 header:
  `X-Spillway-Served-By`(成員 id)、`X-Spillway-Slot`(slot 編號;`local/` 直呼為 `direct`,決策 18)、
  `X-Spillway-Est-Cost`(呼叫前預估的 `cost_usd_p90`,固定 8 位小數;**無預估時為空字串**——header
  一律存在,客戶端以「空值 = 無定價」判斷,不必區分「缺 header」與「無預估」)。

`GET /v1/models`:OpenAI list 格式;依序為 route 名稱(字母序)、成員 `model_name`(字母序,
與前者去重)、`local/<id>`(local 成員,依 id)。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from spillway.api import ApiError, filter_chat_params, redact_echo
from spillway.domain import CallResult, ChatRequest
from spillway.redact import Redactor
from spillway.routing import InvalidEffort, RouteNotFound, RouteOutcome, normalize_effort
from spillway.store import repo

__all__ = ["router"]

_log = logging.getLogger(__name__)

router = APIRouter()
_REDACTOR = Redactor()

HEADER_SERVED_BY = "X-Spillway-Served-By"
HEADER_SLOT = "X-Spillway-Slot"
HEADER_EST_COST = "X-Spillway-Est-Cost"
SLOT_DIRECT = "direct"


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: Any = None


class ChatCompletionBody(BaseModel):
    """OpenAI `chat/completions` 請求;未列出的欄位進 `model_extra`,轉交時只留白名單參數。"""

    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1)
    messages: list[Message] = Field(min_length=1)
    stream: StrictBool = False
    max_tokens: int | None = Field(default=None, ge=1)
    reasoning_effort: Any = None
    tools: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] | None = None

    def to_request(self, effort: str | None) -> ChatRequest:
        return ChatRequest(
            model=self.model,
            messages=[m.model_dump(exclude_unset=True) for m in self.messages],
            max_tokens=self.max_tokens,
            reasoning_effort=effort,
            tools=self.tools,
            metadata=dict(self.metadata or {}),
            stream=self.stream,
            extra=filter_chat_params(self.model_extra),
        )


def format_est_cost(value: float | None) -> str:
    return "" if value is None else f"{value:.8f}"


def format_no_candidate(outcome: RouteOutcome) -> str:
    """決策 31:「嘗試:a:rate_limited, b:error;被篩:c:cooling」。沒有任何一項時說明原因。"""
    tried = ", ".join(f"{a.deployment}:{a.status}" for a in outcome.attempts)
    excluded = ", ".join(f"{dep}:{reason}" for dep, reason in outcome.excluded)
    parts = ["沒有可用的候選成員"]
    parts.append(f"嘗試:{tried}" if tried else "嘗試:無")
    if excluded:
        parts.append(f"被篩:{excluded}")
    # 防線:成員 id 與狀態不應含秘密,仍以預設樣式再遮罩一次
    text, _ = _REDACTOR.redact(";".join(parts))
    return text


def _completion_body(req: ChatRequest, result: CallResult, created: int) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant"}
    if result.tool_calls:
        message["content"] = result.text or None
        message["tool_calls"] = result.tool_calls
        finish = result.finish_reason or "tool_calls"
    else:
        message["content"] = result.text or ""
        finish = result.finish_reason or "stop"
    tin, tout = result.tokens_in or 0, result.tokens_out or 0
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": created,
        "model": req.model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": tin, "completion_tokens": tout, "total_tokens": tin + tout},
    }


@router.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionBody, request: Request) -> JSONResponse:
    if body.stream:
        raise ApiError(400, "streaming_unsupported", "第一版不支援 streaming(spec 1.1.3),請改用 stream: false")
    try:
        effort = normalize_effort(body.reasoning_effort)
    except InvalidEffort:
        raise ApiError(422, "validation_error",
                       "body.reasoning_effort: 只可為 none、low、medium、high、max(或 minimal)") from None
    req = body.to_request(effort)
    state = request.app.state
    try:
        outcome = await state.router.route(req)
    except RouteNotFound:
        raise ApiError(404, "route_not_found", f"找不到 route 或成員:{redact_echo(req.model)}") from None
    except InvalidEffort:   # Router 的防線(決策 40);正規化後不應發生
        raise ApiError(422, "validation_error", "body.reasoning_effort: 不合法") from None
    except Exception as exc:  # noqa: BLE001 — 例如帳本寫入失敗(決策 35);不回顯例外內容
        _log.error("chat/completions 內部錯誤:%s", type(exc).__name__)
        raise ApiError(500, "internal_error", f"內部錯誤({type(exc).__name__})") from None

    if outcome.result is None:
        raise ApiError(503, "no_candidate", format_no_candidate(outcome))

    created = int(state.clock.now().timestamp())
    est = outcome.estimate.cost_usd_p90 if outcome.estimate is not None else None
    headers = {
        HEADER_SERVED_BY: outcome.served_by or "",
        HEADER_SLOT: SLOT_DIRECT if outcome.slot is None else str(outcome.slot),
        HEADER_EST_COST: format_est_cost(est),
    }
    return JSONResponse(_completion_body(req, outcome.result, created), headers=headers)


@router.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    state = request.app.state
    conn = state.conn_factory()
    try:
        routes = sorted(r.name for r in repo.list_routes(conn))
        deps = repo.list_deployments(conn)
    finally:
        conn.close()
    ids: list[str] = []
    seen: set[str] = set()
    for name in [*routes, *sorted({d.model_name for d in deps}),
                 *(f"local/{d.id}" for d in deps if d.kind == "local")]:
        if name not in seen:
            seen.add(name)
            ids.append(name)
    created = int(state.clock.now().timestamp())
    return {
        "object": "list",
        "data": [{"id": i, "object": "model", "created": created, "owned_by": "spillway"}
                 for i in ids],
    }

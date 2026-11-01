"""`/api/routes/{name}/dry-run` 與 `/api/estimate`(CHG-20261101-02 決策 9)。"""
from __future__ import annotations

from typing import Any

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from spillway.api import filter_chat_params
from spillway.api.deps import request_conn
from spillway.domain import ChatRequest
from spillway.effort import InvalidEffort, normalize_effort
from spillway.routing import Plan, RouteNotFound

from .common import _estimate_out, _invalid, _not_found, router
from .routes import _load_route


# ================================================================ dry-run、estimate


class ChatBody(BaseModel):
    """範例請求(OpenAI 形狀);未列出的參數只留 `spillway.api.ALLOWED_CHAT_PARAMS` 白名單內的
    OpenAI 標準參數放進 `ChatRequest.extra`,其餘靜默丟棄(U8,與 `/v1/chat/completions` 同一份過濾)。
    `stream` 不影響 dry-run。"""

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
                           extra=filter_chat_params(self.model_extra))


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
        raise _not_found(f"route 或成員:{model}") from None
    except InvalidEffort:
        raise _invalid("body.reasoning_effort", "必須為 none、low、medium、high、max") from None


@router.post("/routes/{name}/dry-run")
async def dry_run_route(request: Request, name: str, body: ChatBody) -> dict[str, Any]:
    """以範例請求模擬路由(不呼叫上游、不寫帳本);route 不存在 → 404。body 的 `model` 忽略。
    回應為 Plan JSON(見 `_plan_out`)。"""
    with request_conn(request) as conn:
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

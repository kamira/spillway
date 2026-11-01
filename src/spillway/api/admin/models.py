"""管理 API 請求 body 模型:成員、群組、route(含呼叫憑證掃描與 `validate_signals` 的 validator;
CHG-20261101-02 決策 1、4、9)。

寫入驗證規則見 `spillway.api.admin` 套件 docstring;憑證掃描本體在 `secrets.py`。
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from spillway import credentials
from spillway.adapters.base import SignalsError, validate_signals
from spillway.api import redact_echo, validation_message
from spillway.domain import Budget, Deployment
from spillway.store import repo

from .secrets import _secret_message, _secret_paths


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

    @model_validator(mode="after")
    def _check_extra(self) -> CapabilitiesIn:
        hits = _secret_paths(self.model_extra or {}, "capabilities")
        if hits:
            raise ValueError(_secret_message(hits))
        return self


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
            raise ValueError(_secret_message(hits))
        if "signals" in v:          # 決策 4:寫入時即驗證格式(訊息只含欄位路徑與說明,不含值)
            try:
                validate_signals(v["signals"], "params.signals")
            except SignalsError as exc:
                raise ValueError(redact_echo(exc)) from None
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
                "tags 必須是 {all, any, none} 字串清單(" + validation_message(
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
            raise ValueError(redact_echo(f"同一成員出現多次:{', '.join(sorted(set(dup)))}"))
        return v

"""跨模組共用的值物件(spec 7.1;CHG-20261030-02 契約清單)。

位於基礎層,任何層都可 import;store 由資料列建出 `Deployment`,adapter、routing、api 讀它。
`spillway.adapters.base` 轉匯出這些型別,adapter 模組照舊只 import base。改欄位要走修改治理。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

KINDS = ("cli", "api", "local")
# 有額度與冷卻的成員種類(4.2.3、4.5.2);local 不檢查額度與冷卻
CLOUD_KINDS = ("cli", "api")
EFFORTS = ("none", "low", "medium", "high", "max")
CALL_STATUSES = ("ok", "error", "quota_exhausted", "rate_limited")
# 會設冷卻的呼叫結果(4.5.2)
COOLING_STATUSES = ("quota_exhausted", "rate_limited")
# 預算預檢跳過(4.2.5):只出現在帳本,不是 adapter 的回傳值
STATUS_SKIPPED_PRECHECK = "skipped_precheck"
# 契約清單 `usage_ledger.status`(CHG-20261101-02 決策 6:型別維持 frozenset)
LEDGER_STATUSES = frozenset({*CALL_STATUSES, STATUS_SKIPPED_PRECHECK})


@dataclass(frozen=True)
class Budget:
    type: str                       # usd | credit_pool | none
    limit_amount: float | None
    window: str = "monthly"         # monthly | weekly | daily | none
    reset_anchor: int | None = None  # monthly 的重置日,1–28
    exhausted_until: datetime | None = None


@dataclass(frozen=True)
class Deployment:
    id: str
    model_name: str
    kind: str
    provider: str
    model: str
    params: dict[str, Any] = field(default_factory=dict)
    credential_ref: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)  # {ctx, tools, vision, efforts[]}
    enabled: bool = True
    tags: tuple[str, ...] = ()
    budget: Budget | None = None


@dataclass(frozen=True)
class ChatRequest:
    model: str
    messages: list[dict[str, Any]]
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    tools: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    stream: bool = False
    extra: dict[str, Any] = field(default_factory=dict)   # 其餘 OpenAI 參數原樣轉交

    @property
    def has_tools(self) -> bool:
        return bool(self.tools)

    @property
    def has_images(self) -> bool:
        for m in self.messages:
            content = m.get("content")
            if isinstance(content, list) and any(
                isinstance(p, dict) and p.get("type") in ("image_url", "image") for p in content
            ):
                return True
        return False

    @property
    def text_chars(self) -> int:
        n = 0
        for m in self.messages:
            content = m.get("content")
            if isinstance(content, str):
                n += len(content)
            elif isinstance(content, list):
                n += sum(len(p.get("text", "")) for p in content if isinstance(p, dict))
        return n


@dataclass(frozen=True)
class CallResult:
    ok: bool
    text: str
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    status: str          # ok | error | quota_exhausted | rate_limited
    error: str | None
    # spec 7.1(決策 28):附加的可選欄位,放在最後以相容既有建構呼叫;不升 contract_version
    tool_calls: list[dict[str, Any]] | None = None   # 上游回傳的工具呼叫(OpenAI 格式)
    finish_reason: str | None = None

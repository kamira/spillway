"""Adapter 介面與注入給模組的 AdapterContext(spec 7.1;決策 5、13)。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

from spillway.adapters.base.types import CallResult, ChatRequest, Deployment

if TYPE_CHECKING:  # 只為型別標註;模組拿到的是注入的實例,不 import spillway.clock
    from spillway.clock import Clock

CredentialResolver = Callable[[str], str]
"""`credential_ref`(`env:VAR`、`keyring:服務:帳號`)→ 當下解析出的值;失敗時丟例外。"""


@runtime_checkable
class Adapter(Protocol):
    """每個 adapter 模組的 `create(context)` 回傳的實例。兩個方法一律 `async def`。"""

    async def call(self, dep: Deployment, req: ChatRequest, effort: str | None) -> CallResult: ...

    async def health(self, dep: Deployment) -> bool: ...


@dataclass(frozen=True)
class AdapterContext:
    """`create(context)` 拿到的唯一外部依賴(design「Adapter 模組契約」)。

    - `credentials`:憑證解析器;API 模組於呼叫當下以它解析 `dep.credential_ref`,
      並把結果明確傳給 `litellm_call(api_key=...)`。
    - `clock`:`spillway.clock.Clock` 實例(業務時間一律經它取得)。
    - `cli_runner`:子行程執行器;M2 為 None,M3 補上。

    新增欄位不算破壞契約、不升 `contract_version`(決策 13)。
    """

    credentials: CredentialResolver
    clock: "Clock"
    cli_runner: Any = None

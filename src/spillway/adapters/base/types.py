"""轉匯出 `spillway.domain` 的值物件,讓 adapter 模組只需 import `adapters.base`(spec 7.6.3)。"""
from spillway.domain import (  # noqa: F401
    CALL_STATUSES,
    EFFORTS,
    KINDS,
    Budget,
    CallResult,
    ChatRequest,
    Deployment,
)

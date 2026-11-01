"""本模組的 adapter 類別:繼承 `adapters.base.LiteLLMApiAdapter`,不覆寫 `call`/`health`
(CHG-20261101-02 決策 11)。

流程與錯誤歸類全部在基底類別;本模組的差異只在 `__init__.py` 的 `MANIFEST`,以
`LiteLLMAdapter(context, MANIFEST)` 建立。類名保留 `LiteLLMAdapter`,避免與 `adapters.base.Adapter`
Protocol 名稱混淆,模組 `__init__.py` 也不必修改。

`litellm_call`、`model_string`、`probe_http` 匯入到本模組命名空間作為替換點:基底類別先取本模組的
同名屬性,所以在這裡替換(例如測試的 spy)只影響本模組。模組只可 import `adapters.base`(DIR-3)。
"""
from __future__ import annotations

from spillway.adapters.base import LiteLLMApiAdapter, litellm_call, model_string, probe_http

__all__ = ["LiteLLMAdapter", "litellm_call", "model_string", "probe_http"]


class LiteLLMAdapter(LiteLLMApiAdapter):
    """經 LiteLLM 呼叫上游;行為全部繼承自 `LiteLLMApiAdapter`。"""

"""擋下 `mcp` SDK 直接寫到 root logger 的紀錄(CHG-20261102-01 終審修正 A;安全影響「秘密」)。

`mcp.shared.session.BaseSession._receive_loop` 對驗證失敗的 request 呼叫模組層級的
`logging.warning(f"Failed to validate request: {e}")` 與 `logging.debug(f"Message that failed
validation: …")`:紀錄的 logger 名稱是 `root`,不經過 `mcp.*` 具名 logger,所以 `bridge._quiet_loggers`
管不到;root 沒有 handler 時 `logging.warning` 還會先 `basicConfig()`,紀錄因此印到 stderr。pydantic 的
訊息含 `input_value`(request 的片段,例如 `tools/call` 的 `arguments` 為字串時的整段 prompt)。

`install_sdk_root_log_filter()` 在 **root logger 物件**(不是 handler)上掛 `SdkRootRecordFilter`:
`record.name == "root"` 且 `record.pathname` 位於已安裝的 `mcp` 套件目錄內 → 丟棄。具名 logger
(`mcp.*` 等)與其他來源的 root 紀錄不受影響。重複呼叫只掛一次。serve(`create_app`)與
`spillway mcp-stdio`(`bridge.run`)都安裝。不 import `mcp`(只用 `find_spec` 找目錄)。
"""
from __future__ import annotations

import importlib.util
import logging
import os
from functools import lru_cache

__all__ = ["SdkRootRecordFilter", "install_sdk_root_log_filter"]


@lru_cache(maxsize=1)
def _mcp_dir() -> str | None:
    spec = importlib.util.find_spec("mcp")
    if spec is None or not spec.submodule_search_locations:
        return None
    return os.path.realpath(next(iter(spec.submodule_search_locations)))


class SdkRootRecordFilter(logging.Filter):
    """丟掉 `mcp` 套件內的程式直接寫到 root logger 的紀錄。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "root":
            return True
        base = _mcp_dir()
        if base is None or not record.pathname:
            return True
        path = os.path.realpath(record.pathname)
        return not (path == base or path.startswith(base + os.sep))


def install_sdk_root_log_filter() -> None:
    """在 root logger 掛 `SdkRootRecordFilter`(冪等)。"""
    root = logging.getLogger()
    if not any(isinstance(f, SdkRootRecordFilter) for f in root.filters):
        root.addFilter(SdkRootRecordFilter())

"""假 API 上游(CHG-20261107-03 T2;決策 8 ④)。

`tests/fakes/api_upstream` 的 `FakeApiServer` 不 import spillway,所以留在測試行程內(背景執行緒,
只綁 127.0.0.1);每次 run 啟動一次,情境或測試之間 `reset()`。介面見 `tests/fakes/api_upstream/__init__.py`
(`base_url`、`set_mode(model, mode, **kw)`、`reset()`、`requests`)。子行程 serve 經成員的 `api_base`
(`{base_url}/v1`)打到它。

- `FakeApi`:`FakeApiServer` 的別名。
- `shared_fake_api() -> FakeApi`:本行程共用的一個實例(第一次呼叫時啟動,行程結束時停止);
  pytest 用 `tests/blackbox/plugin.py` 的 `bb_fake_api` fixture,behave 在 `before_all` 呼叫。
"""
from __future__ import annotations

import atexit

from tests.fakes.api_upstream.server import FakeApiServer, ModeSpec, RecordedRequest

__all__ = ["FakeApi", "ModeSpec", "RecordedRequest", "shared_fake_api"]

FakeApi = FakeApiServer
_shared: FakeApiServer | None = None


def shared_fake_api() -> FakeApiServer:
    global _shared
    if _shared is None:
        _shared = FakeApiServer().start()
        atexit.register(_shared.stop)
    return _shared

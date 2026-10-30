"""假上游的 pytest fixture(CHG-20261030-02 T5)。

由 `tests/conftest.py` 的 `pytest_plugins = ["tests.fakes.plugin"]` 載入。

`fake_api` 介面(見 `tests/fakes/api_upstream/__init__.py`):
- `base_url`:`http://127.0.0.1:<隨機埠>`
- `set_mode(model, mode, **kw)`:依 model 名稱設定模式;`model="*"` 為未設定 model 的預設
- `reset()`:清除模式與請求紀錄
- `requests`:收到的請求清單(`RecordedRequest`,不含 auth 值)
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.fakes.api_upstream.server import FakeApiServer


@pytest.fixture(scope="session")
def _fake_api_server() -> Iterator[FakeApiServer]:
    server = FakeApiServer().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def fake_api(_fake_api_server: FakeApiServer) -> Iterator[FakeApiServer]:
    _fake_api_server.reset()
    yield _fake_api_server
    _fake_api_server.reset()

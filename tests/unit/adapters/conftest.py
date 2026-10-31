"""三個內建 API 模組的單元/整合測試共用 fixture(CHG-20261030-02 T6)。"""
from __future__ import annotations

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"

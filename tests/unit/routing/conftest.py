"""tests/unit/routing 共用 fixture(CHG-20261030-02 T11 擁有)。輔助函式見 `helpers.py`。"""
from __future__ import annotations

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"

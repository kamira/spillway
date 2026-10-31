"""合約測試共用 fixture(T4 擁有;T6 的 test_call_status.py 共用 anyio_backend)。"""
import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"

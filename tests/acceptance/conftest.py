"""tests/acceptance 共用 fixture 與工具(CHG-20261030-02 T15)。

- 沿用 `tests/integration/conftest.py` 的 `open_spillway`(以 import 註冊為本目錄的 fixture;同一份
  實作,不另寫一套);`anyio_backend` 由根 conftest 提供。
- `test_key_env`:設定 `SPILLWAY_TEST_KEY=sk-test-0000`(api 成員的 `credential_ref` 只讀此變數);
  即 `tests/helpers.py` 的 `fake_key_env`。
- `artifacts_dir`:建立並回傳 `<repo>/artifacts/m2/`(不進版控)。
共用函式(`record_result`、`p95`)在 `tests/acceptance/results.py`。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import open_spillway  # noqa: F401 — fixture 註冊
from tests.acceptance.results import ARTIFACTS


@pytest.fixture
def test_key_env(fake_key_env: str) -> str:
    return fake_key_env


@pytest.fixture
def artifacts_dir() -> Path:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    return ARTIFACTS

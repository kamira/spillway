"""tests/acceptance 共用 fixture 與工具(CHG-20261030-02 T15)。

- 沿用 `tests/integration/conftest.py` 的 `open_spillway`、`anyio_backend`(以 import 註冊為本目錄的
  fixture;同一份實作,不另寫一套)。
- `test_key_env`:設定 `SPILLWAY_TEST_KEY=sk-test-0000`(api 成員的 `credential_ref` 只讀此變數)。
- `artifacts_dir`:建立並回傳 `<repo>/artifacts/m2/`(不進版控)。
共用函式(`record_result`、`p95`)在 `tests/acceptance/results.py`。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import anyio_backend, open_spillway  # noqa: F401 — fixture 註冊
from tests.acceptance.results import ARTIFACTS

TEST_KEY = "sk-test-0000"
TEST_KEY_ENV = "SPILLWAY_TEST_KEY"


@pytest.fixture
def test_key_env(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY)
    return TEST_KEY


@pytest.fixture
def artifacts_dir() -> Path:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    return ARTIFACTS

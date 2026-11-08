"""tests/acceptance 共用 fixture 與工具(CHG-20261030-02 T15;CHG-20261107-03 T3 黑箱化)。

- serve 一律為子行程:`serve_factory`(`tests/blackbox/plugin.py`,經 `SPILLWAY_CMD`)或
  `tests.blackbox.harness.Serve`;本目錄不 import spillway(邊界守門與執行期守門,決策 9)。
  假上游 `fake_api` 由 `tests/fakes/plugin.py` 提供(行程內,不 import spillway;決策 8 ④)。
- `test_key_env`:設定 `SPILLWAY_TEST_KEY=sk-test-0000`(api 成員的 `credential_ref` 只讀此變數;
  `serve_factory` 的子行程環境複製目前環境,因此 serve 看得到);即根 conftest 的 `fake_key_env`。
- `artifacts_dir`:建立並回傳 `<repo>/artifacts/m2/`(不進版控)。
共用函式(`record_result`、`p95`)在 `tests/acceptance/results.py`。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.acceptance.results import ARTIFACTS


@pytest.fixture
def test_key_env(fake_key_env: str) -> str:
    return fake_key_env


@pytest.fixture
def artifacts_dir() -> Path:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    return ARTIFACTS

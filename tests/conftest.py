"""全測試共用的護欄(CHG-20261030-02 T1)。

- `SPILLWAY_HOME` 一律指向 pytest 暫存目錄;沒指向暫存目錄就 fail,避免碰到擁有者的實際資料。
- 清除真實的 API key 環境變數,`env:` 只讀測試專用變數。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from spillway.clock import FakeClock

pytest_plugins = ["tests.fakes.conftest"]

REAL_KEY_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


@pytest.fixture(autouse=True)
def _spillway_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "spillway-home"
    home.mkdir()
    monkeypatch.setenv("SPILLWAY_HOME", str(home))
    for var in REAL_KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    tmp_root = Path(tempfile.gettempdir()).resolve()
    if not Path(os.environ["SPILLWAY_HOME"]).resolve().is_relative_to(tmp_root):
        pytest.fail("SPILLWAY_HOME 沒有指向暫存目錄,拒絕執行")
    return home


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()

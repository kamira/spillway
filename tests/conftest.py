"""全測試共用的護欄(CHG-20261030-02 T1)。

- `SPILLWAY_HOME` 一律指向 pytest 暫存目錄;沒指向暫存目錄就 fail,避免碰到擁有者的實際資料。
- 清除真實的 API key 環境變數,`env:` 只讀測試專用變數。
- `anyio_backend`:全測試唯一的定義,固定 `"asyncio"`(CHG-20261101-02 T11);非同步測試加
  `pytestmark = pytest.mark.anyio`。
- `fake_key_env`:見 `tests/helpers.py`(非 autouse)。
- 真實 CLI 護欄(CHG-20261101-03 T3;決策 19,autouse):每個測試開始前把 PATH 中含真實 `claude`、`codex`
  的目錄移除並斷言 `shutil.which` 只找得到假執行檔(或找不到);測試結束時再斷言一次,測試中把真實
  執行檔放回 PATH 會讓該測試失敗。
- `fake_cli`:在暫存目錄產生假 `claude`、`codex`(`tests/fakes/cli_upstream/`)並放到 PATH 最前面,
  回傳 `FakeCli`(控制檔與記錄檔在 `SPILLWAY_HOME/fake_cli/`)。測試啟動的子行程用
  `tests.fakes.cli_upstream.safe_subprocess_env()` 建環境。
"""
from __future__ import annotations

import os

# 必須在任何 import litellm 之前:用本地價格表,不在 import 時連網抓(Global Constraints)
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
import tempfile
from pathlib import Path

import pytest

from spillway.clock import FakeClock
from tests.fakes.cli_upstream import FakeCli, assert_only_fakes, install, sanitize_path
from tests.helpers import fake_key_env  # noqa: F401 — fixture 註冊(非 autouse)

pytest_plugins = ["tests.fakes.plugin"]

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


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _real_cli_guard(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PATH", sanitize_path(os.environ.get("PATH", "")))
    assert_only_fakes()
    yield
    assert_only_fakes()


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _spillway_home: Path,
             _real_cli_guard: None) -> FakeCli:
    bin_dir = install(tmp_path / "fake-cli-bin")
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")]))
    found = assert_only_fakes()
    assert found == {name: str(bin_dir / name) for name in found}
    return FakeCli(_spillway_home, bin_dir)

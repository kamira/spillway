"""`claude_cli` 模組的合約測試 harness(CHG-20261101-03 決策 18;`tests/contract/test_call_status.py`)。

`call_status_harness(fake_api)` 回傳 `[(Deployment, set_mode)]`:`set_mode(mode)` 把
`test_call_status.py` 的上游模式對應到假 `claude` 控制檔情境(`tests/fakes/cli_upstream/`):

| 模式 | 假 claude 情境 | 預期 status |
|------|----------------|-------------|
| `ok` | `ok` | `ok` |
| `http_402`、`insufficient_quota` | `quota` | `quota_exhausted` |
| `http_429` | `rate_limit` | `rate_limited` |
| `http_500` | `error` | `error` |

`fake_api` 不使用(claude 不走 HTTP)。harness 會:
1. 在 `$SPILLWAY_HOME/fake-cli-bin` 產生假執行檔並放到 `PATH` 最前面。`PATH` 由根 conftest 的 autouse
   護欄 `_real_cli_guard` 以 `monkeypatch.setenv` 管理,測試結束時還原,這裡的修改不會外洩到下一個測試;
   放上去之後再以 `assert_only_fakes()` 確認找不到真實 CLI。
2. `test_call_status.py` 以沒有 `cli_runner` 的 `AdapterContext` 建立模組實例(呼叫只會回 `error`),
   所以把 registry 上 `claude_cli` 實例的 `context` 換成帶 `CliRunner` 與 `data_dir=$SPILLWAY_HOME`
   的版本(只影響該測試模組自己的 registry 實例)。成員不設 `params.cwd`,走預設 `work/claude`。
"""
from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path
from typing import Any

from spillway.adapters.base import CliRunner, Deployment
from tests.fakes.cli_upstream import FakeCli, assert_only_fakes, install

MODULE = "claude_cli"
CONTRACT_TEST_MODULE = "tests.contract.test_call_status"
MODE_TO_SCENARIO = {
    "ok": "ok",
    "http_402": "quota",
    "insufficient_quota": "quota",
    "http_429": "rate_limit",
    "http_500": "error",
}


def _registry_instance() -> Any:
    mod = sys.modules.get(CONTRACT_TEST_MODULE)
    for info in getattr(mod, "MODULES", ()) or ():
        if info.name == MODULE:
            return info.instance
    return None


def attach_runner(instance: Any, data_dir: Path) -> None:
    """把 `instance.context` 換成帶 `CliRunner`(沿用既有的)與 `data_dir` 的 AdapterContext。"""
    ctx = instance.context
    runner = ctx.cli_runner if ctx.cli_runner is not None else CliRunner()
    instance.context = dataclasses.replace(ctx, cli_runner=runner, data_dir=data_dir)


def install_fake_on_path(home: Path) -> FakeCli:
    bin_dir = install(home / "fake-cli-bin")
    os.environ["PATH"] = os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])
    found = assert_only_fakes()
    assert found["claude"] == str(bin_dir / "claude"), found
    return FakeCli(home, bin_dir)


def call_status_harness(fake_api: Any) -> list[tuple[Deployment, Any]]:
    home = Path(os.environ["SPILLWAY_HOME"])
    fake = install_fake_on_path(home)
    instance = _registry_instance()
    if instance is not None:
        attach_runner(instance, home)
    dep = Deployment(id="claude_cli-cli", model_name="claude_cli-cli", kind="cli",
                     provider="claude-cli", model="claude-sonnet-5-5")

    def set_mode(mode: str) -> None:
        fake.set("claude", MODE_TO_SCENARIO[mode])

    return [(dep, set_mode)]

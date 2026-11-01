"""`codex_cli` 模組的 call 狀態 harness(`tests/contract/test_call_status.py`;CHG-20261101-03 T5、決策 18)。

走真實子行程路徑:假 `codex`(`tests/fakes/cli_upstream/`)以 `install()` 產生在 `$SPILLWAY_HOME/fake-cli-bin`
並放到 PATH 最前面(PATH 由根 conftest 的 autouse `_real_cli_guard` 以 monkeypatch 管理,測試結束即還原);
上游模式以控制檔 `$SPILLWAY_HOME/fake_cli/codex.json` 切換:

| 合約模式 | 假 codex 情境 | 期望 status |
|----------|---------------|-------------|
| `ok` | `ok` | `ok` |
| `http_402`、`insufficient_quota` | `quota` | `quota_exhausted` |
| `http_429` | `rate_limit` | `rate_limited` |
| `http_500` | `error` | `error` |

合約測試以 `AdapterContext(credentials, clock)`(沒有 `cli_runner`、`data_dir`)建立模組實例,而 codex_cli
在 `cli_runner=None` 時 `call` 依決策 11 回 `error`。因此本 harness 把合約測試模組 `MODULES` 中 codex_cli 的
`instance` 換成以帶 `CliRunner` 與 `data_dir=$SPILLWAY_HOME` 的 context 建立的實例(只在原實例沒有
`cli_runner` 時替換;每個測試重新替換,`data_dir` 跟著當次的暫存目錄)。
"""
from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path
from typing import Any

from spillway.adapters.base import CliRunner, Deployment
from tests.fakes.cli_upstream import FakeCli, assert_only_fakes, install

MODULE = "codex_cli"
MODE_TO_SCENARIO = {
    "ok": "ok",
    "http_402": "quota",
    "insufficient_quota": "quota",
    "http_429": "rate_limit",
    "http_500": "error",
}


_WIRED: set[int] = set()   # 本 harness 換上的實例的 id()


def _contract_modules() -> list[Any]:
    """已 import 的合約測試模組(`tests.contract.test_call_status`)的 `MODULES` 中屬於 codex_cli 者。"""
    out = []
    for name, mod in list(sys.modules.items()):
        if name.endswith("test_call_status") and mod is not None:
            out += [m for m in getattr(mod, "MODULES", ()) if getattr(m, "name", None) == MODULE]
    return out


def _wire_instances(home: Path) -> None:
    infos = _contract_modules()
    if not infos:
        raise AssertionError("找不到合約測試的 codex_cli 模組資訊(tests.contract.test_call_status.MODULES)")
    for info in infos:
        inst = info.instance
        if inst is None or info.module is None:
            raise AssertionError(f"codex_cli 沒有可用實例:{info.reason}")
        if inst.context.cli_runner is not None and id(inst) not in _WIRED:
            continue   # 合約測試自己已注入 cli_runner:照用
        wired = info.module.create(dataclasses.replace(
            inst.context, cli_runner=CliRunner(term_grace=1.0, drain_timeout=1.0), data_dir=home))
        _WIRED.discard(id(inst))
        _WIRED.add(id(wired))
        info.instance = wired


def call_status_harness(fake_api: Any) -> list[tuple[Deployment, Any]]:
    home = Path(os.environ["SPILLWAY_HOME"])
    bin_dir = install(home / "fake-cli-bin")
    os.environ["PATH"] = os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])
    assert_only_fakes()
    fake = FakeCli(home, bin_dir)
    _wire_instances(home)
    dep = Deployment(id="codex-sub", model_name="codex-sub", kind="cli", provider="codex-cli",
                     model="gpt-5.5", params={"timeout": 30})

    def set_mode(mode: str) -> None:
        fake.set("codex", MODE_TO_SCENARIO[mode])

    return [(dep, set_mode)]

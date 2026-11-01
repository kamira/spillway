"""假 CLI 執行檔與真實 CLI 護欄(CHG-20261101-03 T3;決策 18、19)。

自動測試一律不執行真實的 `claude`、`codex`。實作在 `harness.py`(假執行檔本體為 `fake.py`),
本模組只重新匯出(同 `tests/fakes/api_upstream`)。

假執行檔
- `install(bin_dir)`:在 `bin_dir` 產生 `claude`、`codex` 兩個 `/bin/sh` wrapper,內容為
  `exec <sys.executable> -I <本目錄>/fake.py <name> "$@"`(用目前的直譯器,避開 macOS `/usr/bin/python3` stub)。
  wrapper 第二行是 `FAKE_MARKER`;護欄以它辨識假執行檔。
- 控制檔 `$SPILLWAY_HOME/fake_cli/<claude|codex>.json`,每次呼叫重新讀取(長駐的 serve 也能切換):
  - `scenario`:`SCENARIOS` 之一,預設 `ok`
    - `ok`:claude 印 spec 7.3.5 的 JSON(`result`、`usage.input_tokens`/`output_tokens`、`total_cost_usd`、
      `is_error=false`);codex 印 JSONL 事件(最後的 `turn.completed` 帶 `usage`)並把回應寫進 `-o <檔>`。結束碼 0
    - `quota`、`rate_limit`、`error`:claude 印 `is_error=true` 的 JSON,`result` 為符合 spec 5.3.1 對應訊號的訊息
      (`error` 兩類都不符合),結束碼 1;codex 結束碼 1,stderr 為符合 spec 5.3.1 codex 列的訊息,不寫 `-o`
    - `timeout`:不輸出,睡 `timeout_sleep_seconds`(預設 3600)
    - `non_json`:stdout 為非 JSON 文字,結束碼 0(codex 不寫 `-o`)
    - `hold_until_release`:等到控制檔 `released` 為 true 才照 `ok` 回應(`FakeCli.release()`)
    - `grandchild_holds_stdout`:起一個繼承 stdout 的孫行程(睡 `grandchild_sleep_seconds`,預設 3600)後照 `ok`
      回應並結束;孫行程 pid 記在記錄檔 `grandchild_pid`
  - 等待(`hold_until_release` 最多 120 秒、`timeout`、延遲、孫行程)期間控制目錄被刪除即結束,不留孤兒行程
  - `delay_seconds`:寫完記錄檔後、回應前的延遲(預設 0)
  - 選用:`result`(回應文字/錯誤訊息覆寫)、`input_tokens`、`output_tokens`、`total_cost_usd`、`exit_code`
- 記錄檔:每次呼叫一個 `$SPILLWAY_HOME/fake_cli/records/<name>-*.json`,欄位 `name`、`argv`(不含程式名)、
  `stdin`、`cwd`、`pid`、`scenario`、`spillway_depth`(值或 null)、`env_present`(`PRESENCE_VARS` 各自是否存在,
  不記值)、`finished`、`exit_code`、`grandchild_pid`(若有)。呼叫開始即寫入(可當作「已開始」訊號)。

護欄
- `sanitize_path(path)`:移除 PATH 中空、相對、`~` 開頭,以及含有「非假」`claude`/`codex` 的項目。
- `assert_only_fakes(path)`:`shutil.which` 找到的 `claude`、`codex` 只能是假執行檔或找不到,否則 `RealCliFound`。
- `safe_subprocess_env(base, fake_bin=None)`:給測試啟動的子行程(serve 等)用的環境:PATH 經清理、
  可選擇把假執行檔目錄放到最前面,並斷言。
- pytest:`tests/conftest.py` 的 autouse 護欄與 `fake_cli` fixture;behave:`tests/features/environment.py`。
"""
from tests.fakes.cli_upstream.harness import (
    CLI_NAMES, FAKE_MARKER, FAKE_SCRIPT, PRESENCE_VARS, SCENARIOS, FakeCli, RealCliFound,
    assert_only_fakes, install, is_fake, real_cli_dirs, safe_subprocess_env, sanitize_path,
)

__all__ = [
    "CLI_NAMES", "SCENARIOS", "PRESENCE_VARS", "FAKE_MARKER", "FAKE_SCRIPT", "RealCliFound",
    "install", "is_fake", "real_cli_dirs", "sanitize_path", "assert_only_fakes",
    "safe_subprocess_env", "FakeCli",
]

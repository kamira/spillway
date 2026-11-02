# codex_cli

以擁有者已用官方 Codex CLI 登入的訂閱,經 `codex exec` 子行程處理 task 模式請求(spec 7.4、7.6.9;
CHG-20261101-03 決策 9、11、12)。

| 項目 | 值 |
|------|----|
| provider | `codex-cli` |
| kind / access | `cli` / `subscription-cli` |
| cardinality | `single`(第二個成員 409 `provider_single`,spec 7.6.5) |
| requires | `codex` 執行檔(PATH 上) |
| 花費 | `cost_usd` 一律為 NULL,只記 token(spec 5.2.3);額度類型應設為 `none` |

## 命令

```
codex exec --json --skip-git-repo-check --ignore-user-config -m <model> -o <暫存檔>
      [-c model_reasoning_effort=<值>] <params.extra_args...> -c mcp_servers={} -- <prompt>
```

- 子行程只經 `AdapterContext.cli_runner`(`adapters.base.CliRunner`)啟動:argv 陣列、不經 shell、
  自成 process group;stdin 為 DEVNULL。
- `--ignore-user-config` 與 `-c mcp_servers={}` 由模組固定附加(後者為 `--` 前最後一個 `-c`),停用 codex
  全部 MCP 伺服器(含本閘道),擁有者無法經 `extra_args` 移除或覆寫(決策 11)。擁有者冒煙(2026-11-02)證實
  `-c mcp_servers={}` 與 config.toml 合併而非取代,因此另以 `--ignore-user-config` 不讀 `$CODEX_HOME/config.toml`
  (登入仍用 `CODEX_HOME`;config.toml 其他設定也不套用)。
- `extra_args` 不可含 `--`,也不可含裸位置引數(不以 `-` 開頭、且前一個元素不是不帶 `=` 的選項,
  例如 `["resume"]`),不可以 `-c`/`--config`(含 `-c=`、`-cKEY=`)設定 `mcp_servers`(會合併加回 MCP)、`profile` 或
  `features`,也不可含 `--enable`(可能加回 `codex_apps`)與 `-p`/`--profile`(含 `-pNAME`、`-p=NAME`;疊加 profile 設定檔);違反時該次回 `error`、不起子行程。
- prompt 前固定加 `--`,開頭為 `-` 或 `resume` 的 prompt 不會被當成旗標或子命令。prompt 在 argv 中,
  `ps` 可見;超過系統 ARG_MAX 時該次回 `error`。
- `-o` 暫存檔以 `tempfile.mkstemp` 建立(權限 0600),呼叫結束(含錯誤、逾時、取消)即刪除。

## 環境

以目前環境為底,移除 `OPENAI_API_KEY`、`CODEX_API_KEY`、`OPENAI_BASE_URL`(避免改走按量計費 API 或
繞回本閘道),設定 `SPILLWAY_DEPTH=1`。所有成員 `credential_ref` 為 `env:<名>` 的變數由 Router 另外移除
並用於遮罩輸出。閘道不讀取、不轉發 codex 的登入憑證。

## params

| 鍵 | 說明 | 預設 |
|----|------|------|
| `timeout` | 逾時秒數,必須是正數(bool、非有限值、≤ 0 回 `error`);`null` 為不設上限 | 600 |
| `extra_args` | 附加在固定旗標之後、`--` 之前的引數(字串清單) | `[]` |
| `cwd` | 子行程工作目錄 | 資料目錄下的 `work/codex`(不存在時建立) |
| `signals` | 覆寫上游訊號判定規則(spec 5.3.1) | manifest `default_signals` |

## 行為

- effort(spec 6.3):`none`→`minimal`、`low`/`medium`/`high` 同名、`max`→`xhigh`,以
  `-c model_reasoning_effort=<值>` 傳遞;沒有 effort 時不傳。
- 成功(結束碼 0):回應取 `-o` 檔內容;token 取 stdout JSONL 中最後一個含 `usage` 的事件的
  `input_tokens`、`output_tokens`。
- 上游訊號(spec 5.3.1 codex-cli 列;比對文字為 stderr 加上 stdout JSONL 最後一個錯誤事件的訊息):
  結束碼 ≠ 0 且符合 `usage limit|quota` → `quota_exhausted`;
  符合 `rate limit|429` → `rate_limited`;其他失敗 → `error`。
- `error`:逾時、閘道停機終止、找不到執行檔、`OSError`(含 E2BIG)、引數含 NUL、`extra_args` 不合法、stdout 不是 JSONL、`-o` 檔為空、
  `cli_runner` 未注入、無法決定工作目錄。
- `health()` 與可用模型清單都不起子行程(本模組沒有 `list_models()`)。

## 可用模型(models.yaml)

| id | ctx | tools | vision | efforts |
|----|-----|-------|--------|---------|
| `gpt-5.5` | 272,000 | ✗ | ✗ | low、medium、high、max |
| `gpt-5.4-mini` | 272,000 | ✗ | ✗ | low、medium、high |

## 已知限制

- 真實 codex 的輸出格式以本機 0.163.0 版確認;`--ignore-user-config` 是否確實不載入 config.toml 的 MCP
  由擁有者以真實呼叫冒煙確認(`codex mcp list` 不支援此旗標)。自動測試只用 `tests/fakes/cli_upstream/` 的假執行檔。

# claude_cli

以 Claude Code 訂閱登入執行 `claude -p` 子行程處理 task 請求(spec 7.3、5.2.2、7.6.9;
CHG-20261101-03 決策 9、10、12)。

| 項目 | 值 |
|------|----|
| provider | `claude-cli` |
| kind / access | `cli` / `subscription-cli` |
| cardinality | `single`(全系統只能有 1 個 `claude-cli` 成員,第二個回 409 `provider_single`) |
| requires | `claude` 執行檔(PATH 上) |
| 子行程 | 一律經 `AdapterContext.cli_runner`(`adapters.base.CliRunner`):argv 陣列、不經 shell、自成 process group |

## 命令

```
claude -p --output-format json --model <model> [effort 參數] --strict-mcp-config [params.extra_args...]
```

- prompt 經 stdin 傳入(spec 7.3.1);messages 依 spec 7.5.1 串成 `[System]`、`[User]`、`[Assistant]` 段落。
- `--strict-mcp-config` 由模組**固定附加**,不在 `extra_args` 內;成員自訂 `extra_args` 不會移除它
  (不載入任何 MCP,含本閘道;spec 7.3.3)。
- 不使用 `--bare`(會改用 API 金鑰登入)。

## 環境(決策 9)

以閘道的環境為底,移除 `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL`、
`CLAUDE_CODE_USE_BEDROCK`、`CLAUDE_CODE_USE_VERTEX`、`CLAUDE_CODE_USE_FOUNDRY`,並設 `SPILLWAY_DEPTH=1`。
其餘成員 `credential_ref` 為 `env:<名>` 的變數由 Router 每次嘗試前移除(`CliRunner.extra_env_remove`),
其值作為該次輸出的遮罩對象。官方登入方式照常由子行程繼承,閘道不讀取、不解析任何 CLI 憑證。

## params

| 鍵 | 說明 | 預設 |
|----|------|------|
| `timeout` | 逾時秒數(spec 7.3.6);逾時殺整個 process group,回 `error` | 600 |
| `effort_args` | effort 參數模板,`{effort}` 換成 `effort_map` 的值;設為 `[]` 則不傳 | `["--effort", "{effort}"]` |
| `extra_args` | 附加在 `--strict-mcp-config` 之後的參數 | `[]` |
| `cwd` | 工作目錄(spec 7.3.4) | 閘道資料目錄下的 `work/claude`(不存在就建立) |
| `signals` | 覆寫上游訊號判定規則(spec 5.3.1;格式見 `adapters/base/signals.py`) | manifest `default_signals` |
| `max_concurrency` | 同時進行的子行程上限,由 Router 取得名額(決策 7) | 1 |

## 行為

- effort(spec 6.3):`none` 或未指定時不傳;`low`/`medium`/`high`/`max` 經 `effort_args` 以同名值傳給 `--effort`。
- 成功:`text` 取 JSON `result`;token 取 `usage.input_tokens`/`output_tokens`;花費取 `total_cost_usd`,
  扣抵 `credit_pool` 額度(spec 5.2.2)。`--verbose` 時輸出為事件陣列,取最後一個 `type == "result"` 的物件。
- 上游訊號(spec 5.3.1 claude-cli 列):
  - `quota_exhausted`:`is_error` 為 true 且訊息符合 `credit|usage limit|extra usage`
  - `rate_limited`:訊息符合 `rate limit|overloaded`
  - 其他 `is_error` 為 true 或結束碼 ≠ 0 → `error`
- `error`(決策 12):逾時、輸出非 JSON 或超過上限被截斷、找不到執行檔或其他 `OSError`、被停機終止
  (`error="shutdown"`)、`cli_runner` 未注入、`data_dir` 未注入且沒設 `params.cwd`。
- `health()`:不起子行程,回 True。
- 沒有 `list_models()`:可用模型只來自 `models.yaml`,`GET /api/adapters` 不起子行程。

## 可用模型(models.yaml)

| id | ctx | tools | vision | efforts |
|----|-----|-------|--------|---------|
| `claude-opus-5-5` | 1,000,000 | ✗ | ✗ | low、medium、high、max |
| `claude-sonnet-5-5` | 1,000,000 | ✗ | ✗ | low、medium、high、max |
| `claude-haiku-5-5` | 1,000,000 | ✗ | ✗ | low、medium、high、max |

清單外的模型(例如別名 `opus`、`sonnet`)可在 UI 以「其他(手動輸入)」填入。

## 已知限制

- 輸出格式以本機 claude 2.1.308 確認;版本更新後由擁有者冒煙發現差異。
- plugin 或 claude.ai connector 提供的 MCP 是否也被 `--strict-mcp-config` 排除,由擁有者冒煙確認。
- 子行程仍繼承決策 9 清單以外的環境變數。

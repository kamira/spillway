# Spillway

**溢流閘**：依額度溢流的個人 LLM 路由閘道（credit-aware LLM router）。

訂閱方案附贈的 credit 優先用完，用完才溢流到 API，最後才是 local 模型。對外只暴露 route 名稱，由閘道決定實際使用哪個模型與 effort，並提供 dashboard 顯示各成員的剩餘額度。

![系統架構](docs/images/architecture.png)

## 狀態

M2 路由核心已完成(CHG-20261030-02、整理 CHG-20261101-02)。M3 分兩部分：M3a（CHG-20261101-03）加入訂閱 CLI 成員（claude-cli、codex-cli）、CLI 併發上限、schema v2、存取 token 與 `spillway init`、graceful shutdown；M3b（MCP server、`mcp-stdio`、MCP 設定輸出）與 M4（Web UI）尚未開始。

## 核心概念

| 概念 | 說明 |
| --- | --- |
| 成員 | 一個可呼叫的模型：訂閱 CLI（`claude -p`、`codex exec`）、雲端 API、local 模型 |
| 群組 / Tag | 群組是有名稱的 tag 選擇器；route 可指向群組或直接指向 tag |
| Route | 對外呼叫名稱；可拖曳設定使用順位（slot），並為每個成員設定 effort 與優先權重（高、中、低） |
| 路由順序 | 手動排序優先；未排序時依「訂閱 → API → local」，同一 slot 內的雲端成員依權重負載平衡 |
| 額度 | 美元預算、訂閱額度池、無預算；呼叫前預估 token 與花費，route 可勾選預估額度檢查 |
| Context 記錄 | 預設記錄每次呼叫的請求與回應，保存 7 天（可調整） |
| MCP | 每個群組一個 MCP 端點，並輸出 Claude Code、Codex、Cursor、Gemini CLI 的設定 |

## 文件

- [企劃書](docs/proposal.md)：背景、目標、範圍、技術選型、合規邊界、里程碑、風險
- [規格書](docs/spec.md)：資料模型、路由規則、額度與預估、effort、adapter、MCP、HTTP API、驗收標準
- [UI layout](docs/ui/)：7 個畫面的設計稿

## 技術選型

Python · FastAPI · LiteLLM SDK · SQLite（WAL）

## 初始化與啟動

```bash
spillway init            # 建立資料目錄（含 work/claude、work/codex）、建立或升級資料庫、產生存取 token
spillway serve           # 只綁 127.0.0.1:8787
```

- 資料目錄為環境變數 `SPILLWAY_HOME`，未設定時為作業系統慣例位置（`platformdirs.user_data_dir("spillway")`）。
- 存取 token 只在產生當下顯示一次，資料庫只存 SHA-256 雜湊。`spillway init` 把 token 印在 stdout（其他訊息走 stderr，可用 `TOKEN=$(spillway init)` 取得）；已有 token 時只提示「已存在，未變更」並以 0 結束，要重新產生請執行 `spillway init --rotate` 或呼叫 `POST /api/settings/token/rotate`（回應 `{"token": "..."}`，舊 token 失效）。沒執行過 `init` 時，`serve` 首次啟動會產生 token 並在 stderr 印一行；若把 stderr 導到檔案，檔案中會留有這一次明文。M3a 尚未以 token 驗證任何請求（M3b 的 `/mcp/*` 才使用）。
- `serve` 啟動前會先確認埠可綁定，再取得資料目錄鎖 `spillway.lock`；任一失敗即結束，不開啟資料庫。同一個資料目錄同時只能有一個 `serve`（`init` 也需要這把鎖，`serve` 執行中時請改用 rotate API）。

## CLI 成員（訂閱）

閘道以官方 CLI 子行程使用訂閱額度：claude-cli 成員執行 `claude -p --output-format json`（prompt 經 stdin），codex-cli 成員執行 `codex exec --json`。每個 provider 只能建立 1 個成員（第二個回 409 `provider_single`）。

1. 先以官方 CLI 自行登入訂閱（`claude`、`codex` 的登入流程）。閘道不讀取、不解析、不轉發任何 OAuth token 或 CLI 憑證檔，只呼叫官方 CLI；官方登入用的環境變數照常傳給子行程。
2. 在管理 API 建立成員（`kind: cli`、`provider: claude-cli` 或 `codex-cli`）。claude-cli 的花費取回應的 `total_cost_usd`，額度類型用 `credit_pool`；codex-cli 不回報金額（`cost_usd` 為 NULL），額度類型用 `none`，可用性只依上游的用完或限流訊號。
3. CLI 成員只用於 task 模式（`chat` route 不選 CLI 成員；請求可帶 `metadata.mode = task`）。

子行程的環境以閘道的環境為底，並：

| 項目 | 處理 |
| --- | --- |
| claude 子行程移除 | `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL`、`CLAUDE_CODE_USE_BEDROCK`、`CLAUDE_CODE_USE_VERTEX`、`CLAUDE_CODE_USE_FOUNDRY` |
| codex 子行程移除 | `OPENAI_API_KEY`、`CODEX_API_KEY`、`OPENAI_BASE_URL` |
| 兩者另移除 | 所有成員 `credential_ref` 為 `env:<變數名>` 的變數；這些變數的值也會在帳本與 payload 中被遮罩 |
| 兩者設定 | `SPILLWAY_DEPTH=1`（避免遞迴呼叫本閘道） |
| MCP | claude 固定加 `--strict-mcp-config`；codex 固定加 `--ignore-user-config` 與 `-c mcp_servers={}`，用意是不載入任何 MCP；`params.extra_args` 無法移除這些旗標，也不可以 `-c` 設定 `mcp_servers`、`profile`、`features`，或用 `--enable`、`-p`/`--profile`。擁有者冒煙證實不加 `--ignore-user-config` 時 codex 會載入 config.toml 的 MCP 與 ChatGPT 帳號的 `codex_apps`（含 GitHub 寫入等工具），加上後兩者皆不載入。codex 的 `-c mcp_servers={}` 經擁有者冒煙證實會與 `~/.codex/config.toml` 合併而非取代，所以閘道的 codex 子行程不讀 config.toml（登入不受影響，但 config.toml 中的其他設定也不套用）。**待確認**：claude 的 plugin／connector 提供的 MCP 是否也被 `--strict-mcp-config` 排除（CHG-20261101-03 已知限制） |

其他說明：

- 未設定 `params.cwd` 時，工作目錄為資料目錄下的 `work/claude`、`work/codex`。逾時預設 600 秒（`params.timeout`），逾時時整個子行程群組（含它啟動的程式）會被終止。
- 每個 CLI 成員同時執行的子行程數上限為 `params.max_concurrency`（預設 1）；額滿時本次跳過、由下一個成員接手，不排隊、不寫帳本。
- 上游回報額度用完時冷卻到下一個重置點，限流時冷卻 60 秒；冷卻狀態顯示在 `/api/deployments`、`/api/dashboard` 的 `cooling_until`、`cooling_reason`（到期後為 null），可用 `POST /api/deployments/{id}/clear-cooldown` 手動清除。
- 已知限制：codex 的 prompt 放在命令列引數，`ps` 可看見，過長時該次呼叫失敗；子行程仍繼承上表以外的環境變數。

## 升級與備份

新版會在 `serve` 或 `init` 開啟資料庫時自動把 schema v1 升級為 v2（冷卻改存 `cooldowns` 表）：

1. 升級前**先停止舊版 `serve`**（舊版不使用 `spillway.lock`，無法偵測）。
2. 升級前會以 SQLite backup API 備份為 `spillway.db.v1.bak`（已存在時另存 `spillway.db.v1.bak.<UTC 時間戳記>`，不覆寫）；資料庫已有外鍵違反、備份失敗或升級失敗時都不會改動原檔，並以非 0 結束（訊息含備份位置）。
3. 回滾：停止 `serve`，刪除 `spillway.db`、`spillway.db-wal`、`spillway.db-shm`，把 `spillway.db.v1.bak` 改名為 `spillway.db`，並回退到舊版程式。v2 期間寫入的資料會遺失；v2 資料庫無法由舊版開啟。

## 停機

按 Ctrl-C 或送 SIGTERM 時，閘道停止接受新請求、最多等 30 秒讓進行中的請求完成，之後終止仍在執行的 CLI 子行程（連同它們啟動的程式），該次嘗試記為 `error="shutdown"`。停機中再按一次 Ctrl-C 會立即終止子行程並結束（該次帳本列可能來不及寫入）。環境變數 `SPILLWAY_TEST_GRACEFUL_TIMEOUT` 僅供測試，正式使用請勿設定。

## 相容性

`/v1/chat/completions` 除了 `model`、`messages`、`stream`、`max_tokens`、`reasoning_effort`、`tools`、`metadata` 這些明列欄位，其他參數只轉交白名單內的 OpenAI 標準參數（`spillway.api.ALLOWED_CHAT_PARAMS`），名單外的鍵一律靜默丟棄、不報錯。

| 參數 | 處理 |
| --- | --- |
| `functions`、`function_call`（OpenAI 舊版函式呼叫） | 丟棄，不轉交上游；請改用 `tools` 與 `tool_choice` |
| 其他不在白名單內的參數 | 丟棄，不轉交上游 |

## 使用範圍

僅供擁有者本人在本機使用。訂閱帳號只透過官方 CLI 呼叫，不讀取、不轉發 OAuth token，不集中多個帳號。

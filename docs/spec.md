# Spillway — 規格書

2026-10-29 · Weiss Lin

修訂：2026-10-29 CHG-20261029-01（adapter 模組化、可用模型清單、後台選單、模組唯一或多個）

修訂：2026-10-30 CHG-20261030-02（7.1 Adapter 介面改為非同步；CallResult 加 tool_calls、finish_reason；5.3.1 加 anthropic 額度與過載訊號）

修訂：2026-11-01 CHG-20261101-03（schema v2：冷卻移至 `cooldowns` 表，`budgets` 移除 `exhausted_until`（2.2、2.3、4.5.2、5.3.2、5.3.3、9.2）；4.2.4 CLI 成員併發上限；6.3.2 模組 `default_params.effort_args` 視為已設定；7.3.2 子行程環境移除清單；7.4.3 固定以 `-c mcp_servers={}` 停用全部 MCP；9.2.4 成員與總覽回應的 `cooling_until`、`cooling_reason`）

修訂：2026-11-02 CHG-20261102-01（8.1 MCP server：群組端點與 session 綁定、群組名 `all` 保留、檢查順序、401 `unauthorized`、遞迴防護 403、工具輸出與錯誤結果、`list_changed` 送達範圍、`mcp-stdio` 結束碼；8.2 設定輸出一律以 `SPILLWAY_TOKEN` 參照 token、回應 `variants`／`notes`、模板與 `TOOL_FORMATS`、8.2.4 改為實作的實際輸出；新增 9.1.4 Host／Origin；9.2 `/api/mcp/config` 參數、`/mcp/<group>`、新增 9.2.5 群組名 `all` 保留；第 10 節 8 個畫面、UI-5 刪除「內嵌 token 切換」、新增 UI-8；AC-9、AC-11 改寫；新增 12.3 AC-22）

修訂：2026-11-02 CHG-20261102-02（3.1 設定檔規則：`merge`／`replace` 語意改為新增或整筆取代並刪除檔案外實體、`targets` 形狀、逐欄匯出規則、匯入請求邊界、嚴格解析、驗證與欄位路徑、套用順序、自動備份、設定檔為可信輸入與 `privileged`、`dry_run`、回應；3.2 範例 `limit` → `limit_amount` 並補欄位；8.1.2 `mcp-stdio` 依 body 且錯誤碼與狀態碼成對判斷拒絕、token 去除前後空白；8.1.3 token 去除前後空白；8.2.1 移除頂層 `format`、`path`、`content`；9.1.4 重複 `Host` 改寫為 400／403 兩路徑；9.2 新增 `/api/ledger/export.csv`、`GET /api/settings`，`/api/config/import` 註明 `mode` 必填與 `dry_run`；9.2.5 群組名稱規則；新增 9.2.6 帳本 CSV、9.2.7 設定總覽；第 10 節 UI-8 名稱「API 參考」）

修訂：2026-11-03 CHG-20261103-01（3.1.12、3.1.14 `privileged` 加入既有成員的 `provider` 變更；9.2 新增 `POST /api/groups/preview`、`GET /api/routes/{name}/effective-order`、`GET /api/mcp/catalog`，`/api/ledger` 與 `/api/ledger/export.csv` 加參數 `min_depth`，`/api/dashboard` 加近 7 天日均花費；新增 9.2.8；第 10 節首段改寫為靜態頁經 `/api` 取資料、網址 `/ui/`、安全標頭與不連外；UI-1 不提供期間切換；UI-3 編輯 tag 不做改名；UI-7 匯入確認流程、token 只顯示一次與精靈網址；UI-8 版面；10.2.2 LiteLLM provider 欄位與已移除模組的顯示；10.2.6 保留能力清單外的既有 effort）

修訂：2026-11-07 CHG-20261107-03（第 12 節註明黑箱驗收方式；新增第 13 節「測試控制介面（僅測試模式）」：開啟條件與資料目錄條件、啟動期旋鈕、`/__test__/*` 端點、形狀與錯誤碼、保存排程 STEPPED 與到期判斷式、seed 為不透明值；新增第 14 節「行程契約」：`serve`／`init` 結束碼、SIGINT／SIGTERM、`TOKEN_NOTICE` 單行的位元組格式、stderr 承諾的行、就緒判定）

修訂：2026-11-08 CHG-20261108-01（新增第 15 節「跨實作一致性契約」；4.5.3 補 `no_candidate` 訊息形狀與字面值；9.1.2 補 HTTP 422 訊息形狀；5.2.1、5.4.2、5.4.4、7.1、7.2、NFR-7、13.2、14.3.5 標註 Python 版專屬或與第 15 節的關係）

## 1 範圍與名詞定義

本規格定義單人使用的 LLM 路由閘道之資料模型、路由規則、介面與驗收標準；目的、選型與風險見企劃書。

### 1.1 本規格不涵蓋

1.1.1 多使用者、權限角色、對外開放的服務。

1.1.2 訂閱 OAuth token 的讀取、轉發，或模擬官方客戶端請求格式。

1.1.3 Streaming 回應（第一版）。

### 1.2 名詞

| 名詞 | 定義 |
| --- | --- |
| 成員（deployment） | 一個可呼叫的模型實體：類型、供應商、上游模型 id、額度、tag |
| 類型（kind） | `cli`（訂閱，經官方 CLI）、`api`（雲端 API key）、`local`（本機模型） |
| Tag | 附在成員上的字串標籤，用於選取成員 |
| 選擇器 | `{all, any, none}` 三個 tag 清單組成的條件 |
| 群組 | 有名稱的選擇器 |
| Route | 對外呼叫的名稱；指定群組或選擇器，及各成員的 effort 與優先順序 |
| 優先級（slot） | Route 內的一個順位，由使用者拖曳決定；不同 slot 依序嘗試，同一 slot 內的雲端成員依優先權重（high、medium、low 三級）負載平衡 |
| Effort | 統一等級 `none`、`low`、`medium`、`high`、`max` |
| 額度類型 | `usd`（美元預算）、`credit_pool`（訂閱附贈額度池）、`none` |
| Adapter 模組 | 一種 AI、一種接入方式一個模組（例：Claude 訂閱 CLI 與 Anthropic API 是兩個模組）；自帶 manifest 與可用模型清單，啟動時自動發現（7.6） |
| 可用模型清單 | 模組宣告的可選模型與各模型的能力預設，供後台選單使用（7.6.4） |
| Cardinality | 模組宣告同一 `provider` 可建立的成員數：`single`（至多 1 個）或 `multiple`（不限） |

## 2 資料模型

資料庫為單一 SQLite 檔案，初始化時只建立表結構，不寫入任何資料列。

### 2.1 建置規則

2.1.1 連線後必須執行 `PRAGMA journal_mode=WAL` 與 `PRAGMA foreign_keys=ON`。

2.1.2 時間欄位必須以 UTC ISO 8601 字串儲存（精度至秒）。

2.1.3 憑證不可寫入資料庫；`credential_ref` 只存 `env:變數名` 或 `keyring:服務:帳號`。

2.1.4 同一 `provider` 的成員數由處理該 `provider` 的模組所宣告的 cardinality 決定：`single` 至多 1 筆，`multiple` 不限；系統必須於建立成員與匯入設定時，在同一交易內檢查（7.6.5）。

2.1.5 `usage_ledger` 不可存入 prompt 或回應內容；內容只可存於 `usage_payloads`（2.4）。

### 2.2 表關係

| 表 | 用途 | 關聯 |
| --- | --- | --- |
| `deployments` | 成員 | — |
| `deployment_tags` | 成員的 tag | N:1 `deployments` |
| `budgets` | 成員的額度設定 | 1:1 `deployments` |
| `cooldowns` | 成員的冷卻狀態（上游回報用完或限流時設定，執行期狀態；不屬於額度設定） | 1:1 `deployments` |
| `groups` | 命名選擇器 | — |
| `routes` | 對外名稱、目標、context 記錄與預估檢查設定 | `target` 參照 `groups.name` 或內嵌選擇器 |
| `route_targets` | Route 內各成員的 slot、順位、effort、優先權重 | N:1 `routes`、N:1 `deployments` |
| `usage_ledger` | 每次嘗試的紀錄，含預估與實際 token、花費 | 以 `deployment_id` 對應成員 |
| `usage_payloads` | 每次嘗試的請求與回應內容 | 1:1 `usage_ledger` |
| `settings` | 存取 token 雜湊、埠號、context 記錄預設與保存天數 | — |

### 2.3 DDL

```sql
CREATE TABLE deployments (
  id             TEXT PRIMARY KEY,
  model_name     TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK (kind IN ('cli','api','local')),
  provider       TEXT NOT NULL,
  model          TEXT NOT NULL,
  params         TEXT NOT NULL DEFAULT '{}',   -- JSON
  credential_ref TEXT,
  capabilities   TEXT NOT NULL DEFAULT '{}',   -- JSON {ctx, tools, vision, efforts[]}
  enabled        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE deployment_tags (
  deployment_id TEXT NOT NULL REFERENCES deployments(id) ON DELETE CASCADE,
  tag           TEXT NOT NULL,
  PRIMARY KEY (deployment_id, tag)
);

CREATE TABLE budgets (
  deployment_id   TEXT PRIMARY KEY REFERENCES deployments(id) ON DELETE CASCADE,
  type            TEXT NOT NULL CHECK (type IN ('usd','credit_pool','none')),
  limit_amount    REAL,
  window          TEXT NOT NULL DEFAULT 'monthly'
                  CHECK (window IN ('monthly','weekly','daily','none')),
  reset_anchor    INTEGER           -- monthly 的重置日，1–28
);

CREATE TABLE cooldowns (
  deployment_id   TEXT PRIMARY KEY REFERENCES deployments(id) ON DELETE CASCADE,
  exhausted_until TEXT NOT NULL,    -- 上游回報用完或限流時設定
  reason          TEXT NOT NULL CHECK (reason IN ('quota_exhausted','rate_limited'))
);

CREATE TABLE groups (
  name     TEXT PRIMARY KEY,
  selector TEXT NOT NULL            -- JSON {all[], any[], none[]}
);

CREATE TABLE routes (
  name   TEXT PRIMARY KEY,
  target TEXT NOT NULL,             -- JSON {group} | {tags:{all,any,none}}
  mode   TEXT NOT NULL DEFAULT 'chat' CHECK (mode IN ('chat','task')),
  tags   TEXT NOT NULL DEFAULT '[]',
  record_context  INTEGER,            -- NULL = 沿用全域；1 開；0 關
  budget_precheck INTEGER NOT NULL DEFAULT 0  -- 1 = 啟用預估額度檢查（4.2.5）
);

CREATE TABLE route_targets (
  route_name    TEXT NOT NULL REFERENCES routes(name) ON DELETE CASCADE,
  deployment_id TEXT NOT NULL REFERENCES deployments(id) ON DELETE CASCADE,
  slot          INTEGER NOT NULL,   -- 使用者拖曳決定的優先級，小者先
  position      INTEGER NOT NULL,   -- slot 內的顯示與 local 嘗試順序
  effort        TEXT CHECK (effort IN ('none','low','medium','high','max')),
  weight        TEXT NOT NULL DEFAULT 'medium'
                CHECK (weight IN ('high','medium','low')),
  PRIMARY KEY (route_name, deployment_id)
);

CREATE TABLE usage_ledger (
  id             INTEGER PRIMARY KEY,
  ts             TEXT NOT NULL,
  route_name     TEXT,
  deployment_id  TEXT,
  effort         TEXT,
  tokens_in      INTEGER,
  tokens_out     INTEGER,
  cost_usd       REAL,
  status         TEXT NOT NULL,     -- ok | error | quota_exhausted | rate_limited | skipped_precheck
  slot           INTEGER,
  fallback_depth INTEGER NOT NULL DEFAULT 0,
  latency_ms     INTEGER,
  est_tokens_in      INTEGER,
  est_tokens_out_p50 INTEGER,
  est_tokens_out_p90 INTEGER,
  est_cost_usd_p90   REAL,
  error          TEXT
);
CREATE INDEX ix_ledger_dep_ts ON usage_ledger(deployment_id, ts);

CREATE TABLE usage_payloads (
  ledger_id     INTEGER PRIMARY KEY REFERENCES usage_ledger(id) ON DELETE CASCADE,
  ts            TEXT NOT NULL,
  request_json  TEXT NOT NULL,      -- 實際送出的 messages；CLI 為轉換後的 prompt
  response_text TEXT,
  bytes         INTEGER NOT NULL,
  truncated     INTEGER NOT NULL DEFAULT 0,
  redacted      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_payloads_ts ON usage_payloads(ts);

CREATE TABLE settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
```

### 2.4 Context 記錄

2.4.1 全域預設由 `settings.record_context_default` 決定，初始值為 `1`（開啟）。

2.4.2 Route 的 `record_context` 為 NULL 時沿用全域；為 0 或 1 時以 route 為準。`local/` 直呼與隱含 route 沿用全域。

2.4.3 每次嘗試（含 fallback 中失敗者）各存一列；單筆上限為 `settings.context_max_bytes`，初始值 262,144 bytes，超過時截斷並設 `truncated = 1`。

2.4.4 寫入前必須以遮罩規則取代疑似金鑰字串（預設：`sk-` 開頭的字串、` Bearer  ` 之後的值、`credential_ref` 解析出的值）；有命中時設 `redacted = 1`。

2.4.5 保存天數由 `settings.context_retention_days` 決定，初始值為 7，於 UI-7 設定；系統必須每小時刪除 `ts` 早於保存期限的列。設為 0 表示不保存內容。

2.4.6 CLI 成員只記錄送入的 prompt 與最終結果文字，不記錄工具呼叫過程。

2.4.7 匯出設定檔不可包含 `usage_payloads`。

## 3 設定檔格式

設定檔為 YAML，用於匯入、匯出與版控；內容與資料庫一一對應，帳本不在其中。

### 3.1 規則

3.1.1 匯出檔不可含任何憑證值，只可含 `credential_ref`。

3.1.2 匯入必須在單一交易內完成；任一欄位驗證失敗時整批不寫入，並回傳錯誤欄位路徑。

3.1.3 匯入模式必須二擇一：`merge`（以成員 `id`、群組與 route 的名稱為鍵新增或整筆取代；檔案外的實體不動）、`replace`（檔案內的實體同樣新增或整筆取代，並刪除檔案外的成員、群組、route；帳本保留）。兩種模式皆不先清空設定表（3.1.10）。

3.1.4 `targets` 為 list（slot）of list（position），每項 `{deployment, effort?, weight?}`，格式同 9.3 的 `slots` 項目；外層清單順序即 slot 順序，內層清單順序即 `position`；slot 不可為空清單；同一 route 的 `targets` 不可重複同一成員；`deployment` 必須是檔案中（merge 時另含資料庫中）存在的成員，不要求屬於 route 目標（不屬於目標者由路由略過，4.3）。

3.1.5 未列 `targets` 的 route，系統依 4.3 的預設順序產生 slot（匯入時清除該 route 既有的手動排序）。

3.1.6 匯出規則（逐欄；匯出 → 空資料庫以 `replace` 匯入 → 再匯出，兩檔必須逐位元組相同）：

| 層級 | 排序與欄位順序 | 省略條件 | 備註 |
| --- | --- | --- | --- |
| 頂層 | `version`、`settings`、`deployments`、`groups`、`routes` | 一律輸出；空值寫 `{}`／`[]` | `version` 為 `1` |
| `settings` | `record_context_default`、`context_retention_days`、`context_max_bytes` | 等於初始值（2.4）的鍵 | 只含這三個鍵；`record_context_default` 為 bool，其餘為整數 |
| `deployments` | 依 `id` 排序；`id`、`model_name`、`kind`、`provider`、`model`、`params`、`credential_ref`、`capabilities`、`enabled`、`tags`、`budget` | `params == {}`、`credential_ref` 為 null、`capabilities == {}`、`enabled` 為 true、`tags == []`、沒有額度設定 | `params`、`capabilities` 照資料庫原樣輸出（不遞迴省略）；`tags` 依存放排序 |
| `budget` | `type`、`limit_amount`、`window`、`reset_anchor` | `limit_amount`、`reset_anchor` 為 null | `type: none`、`window: none` 不省略 |
| `groups` | 依名稱排序；值為 `{all, any, none}` | 空清單 | — |
| `routes` | 依名稱排序；`target`、`mode`、`tags`、`record_context`、`budget_precheck`、`targets` | `mode == chat`、`tags == []`、`record_context` 為 null、`budget_precheck` 為 false、沒有手動排序 | `record_context: false` 不省略；`target` 為 `{group: <名稱>}` 或 `{tags: {all, any, none}}`（全空時為 `{tags: {}}`，不省略）；`tags` 保留原順序與重複值 |
| `targets` 項目 | `deployment`、`effort`、`weight` | `effort` 為 null、`weight == medium` | `effort: none` 不省略；slot 依資料庫的 slot 遞增輸出，中間的空號重新編號 |

匯出檔不含：存取 token 雜湊、冷卻（`cooldowns`）、帳本（`usage_ledger`）、`usage_payloads`（2.4.7）、埠號與其他 `settings` 鍵。YAML 輸出不使用 anchor／alias，行尾為 `\n`，文字為 UTF-8（非 ASCII 字元不跳脫）；含 U+0085、U+2028、U+2029 的字串以雙引號輸出。`GET /api/config/export` 回應 `Content-Type: application/yaml; charset=utf-8`、`Content-Disposition: attachment; filename="spillway-config-<UTC yyyymmddThhmmssZ>.yaml"`、`Cache-Control: no-store`、`X-Content-Type-Options: nosniff`。既有成員的 `params` 或 `capabilities` 含疑似憑證的鍵名或值（現行寫入規則之前寫入的舊列）時，匯出必須回傳 HTTP 422，錯誤碼 `validation_error`，訊息只列位置（例 `deployments[<id>].params.<路徑>`），不含值。

3.1.7 匯入請求：`POST /api/config/import?mode=merge|replace[&dry_run=true]`。`mode` 必填，缺少或不是 `merge`、`replace` 時回傳 HTTP 422。`Content-Type` 必須為 `application/yaml`、`application/x-yaml` 或 `text/yaml`（參數只可為 `charset=utf-8`），其他或未帶時回傳 422；body 上限 1 MiB（1,048,576 bytes），`Content-Length` 超過時不讀取即回傳 422，未帶 `Content-Length`（chunked）時累計超過上限立即回傳 422；body 以 UTF-8 解碼（可帶 BOM），失敗時回傳 422。以上錯誤碼皆為 `validation_error`；Host／Origin 依 9.1.4。

3.1.8 解析規則（嚴格）：不支援 anchor／alias 與任何明確 tag（含 `!!binary`、`!!set`）；巢狀深度上限 32 層；同一 mapping 不可有重複鍵；鍵必須是字串；日期與時間值視為字串；bool 只認 `true`、`false`（不分大小寫），`on`、`yes` 等為字串；頂層必須是 mapping 且 `version` 為 `1`；檔案格式未定義的鍵一律拒絕（`params` 與 `capabilities` 的內容除外，兩者照管理 API 的規則保存）。違反時回傳 HTTP 422，錯誤碼 `validation_error`；YAML 語法錯誤的訊息只含行、列與問題描述，不可回顯輸入內容。

3.1.9 驗證：檔案中的成員、群組、route 與 `settings` 必須通過與管理 API 相同的規則（9.2：`credential_ref` 格式、`params` 不可含憑證鍵名或值、`params.signals`、群組名稱 9.2.5、route 名稱、`targets` 不可重複成員；另檢查重複的成員 `id`）。解析與欄位驗證階段一次回傳全部錯誤（上限 20 筆，以「; 」串接），寫入後的檢查（3.1.10）回傳第一筆；訊息不回顯輸入值。欄位路徑：list 索引為 0 起算的 `[i]`；鍵符合 `^[A-Za-z0-9_-]+$` 時寫 `.鍵`，否則寫 `["…"]`（JSON 字串跳脫）；例 `deployments[1].kind`、`routes["a.b"].targets[0][1].deployment`。

3.1.10 套用：系統必須在單一寫入交易內依序 ① 檢查 `targets` 參照的成員存在（3.1.4）；② `replace` 時寫入自動備份（3.1.11）；③ 新增或整筆取代檔案中的成員（含 tag、額度）、群組、route，每條檔案中的 route 都以檔案的 `targets` 取代其手動排序（未列時清除，3.1.5）；`settings` 只覆寫檔案中出現的鍵；④ 只限 `replace`：刪除檔案外的 route、群組、成員（連同其 tag、額度、冷卻與排序），並將檔案中沒有的 `settings` 鍵（3.1.6 的三個鍵）回到初始值；⑤ 對最終狀態檢查：route 的 `target.group` 必須存在（否則 422，路徑 `routes.<名稱>.target.group`），`single` 模組的 provider 至多 1 個成員（否則 409 `provider_single`，2.1.4、9.2.3）。任一步失敗時整批回滾。帳本、`usage_payloads`、存取 token 雜湊與其他 `settings` 鍵不受影響；仍存在的成員保留冷卻狀態，被刪除成員的冷卻一併刪除。成功後（`dry_run` 除外），系統必須讓受影響成員的健康快取失效，並發送 `notifications/tools/list_changed`（8.1.5）。

3.1.11 自動備份：`replace`（非 `dry_run`）在同一交易的寫鎖內、刪除之前，將匯入前的設定以 3.1.6 的格式寫入資料目錄下的 `backups/config-<UTC yyyymmddThhmmssZ>.yaml`（同名已存在時加 `-2`、`-3`…，不覆寫）；目錄新建時權限 0700、檔案 0600；`backups` 為 symlink 或不是目錄時回傳 HTTP 500，錯誤碼 `internal_error`，不匯入。備份不會自動清理。`replace` 在刪除任何資料前，須先在同一寫鎖內檢查匯入前的設定能通過 3.1.6 的憑證鍵名掃描與 3.1.9 的解析；不能通過時回傳 HTTP 422，錯誤碼 `validation_error`，列出位置（不含值），不刪除、不寫備份；`dry_run=true` 回傳同一個 422。因此 `replace` 寫下的備份一定可以通過匯入，以 `replace` 匯入備份檔即可還原設定。要繼續時，先以管理 API 修正所列資料列再重試。

3.1.12 設定檔是可信輸入：匯入等同以擁有者權限執行的設定（可改變憑證參照、供應商、上游位址與 CLI 參數），只可匯入可信來源。回應的 `privileged` 列出匯入後相對匯入前新增或變更了下列欄位的成員 `id`（依 `id` 排序；只含 id，不含值；被刪除的成員不列）：`credential_ref`；既有成員的 `provider`（改變憑證送往何處；新成員只看其餘欄位）；`params` 頂層的 URL 類鍵（`api_base`、`base_url`、`url`、`endpoint`，或以 `_url`、`_base`、`_endpoint` 結尾）；`kind: cli` 成員的 `params.extra_args`、`params.effort_args`、`params.cwd`。`params.timeout` 等其他鍵不列入。`dry_run=true` 時系統同樣解析、驗證、套用並檢查，之後回滾：不寫入、不寫備份、不讓健康快取失效、不發送 `list_changed`；回應同樣的計數與 `privileged`，`backup` 為 null；驗證失敗時回傳與非 `dry_run` 相同的錯誤。伺服端不強制確認 privileged 變更。

3.1.13 匯入成功回應：`{"mode", "dry_run", "created", "updated", "deleted", "backup", "privileged"}`；`created`、`updated`、`deleted` 各為 `{"deployments", "groups", "routes"}` 計數（`updated` 為檔案中且匯入前已存在的實體數，不論內容是否改變；`merge` 的 `deleted` 皆為 0）；`backup` 為 `backups/` 下的檔名，`merge` 與 `dry_run` 為 null。

3.1.14 已知限制：① 備份不自動清理，不驗證 `credential_ref` 指向的變數或 keyring 項目是否存在；② 3.1.10 ⑤ 的最終狀態檢查涵蓋整個資料庫，既有資料已違反規則（route 指向不存在的群組、`single` 群組有兩個同 provider 成員）時，每一次 `merge` 匯入都被擋下。（原 ③「改變成員的 `provider` 不列入 `privileged`」已由 CHG-20261103-01 刪除，見 3.1.12。）

### 3.2 範例

範例是合法的匯入檔；匯出時省略預設值（例如 `record_context_default: true`、`context_retention_days: 7`、`enabled: true`）。範例鍵順序不代表匯出順序；匯出的鍵順序與排序依 3.1.6 的匯出規則。

```yaml
version: 1
settings:
  record_context_default: true    # 預設開啟
  context_retention_days: 7       # 預設 7 天
  context_max_bytes: 262144       # 預設 262144（256 KiB）
deployments:
  - id: claude-sub
    model_name: sonnet
    kind: cli
    provider: claude-cli
    model: claude-sonnet-5-5
    params: {extra_args: [--verbose]}
    tags: [coding, review]
    capabilities: {ctx: 200000, tools: true, efforts: [low, medium, high, max]}
    budget: {type: credit_pool, limit_amount: 100, window: monthly, reset_anchor: 1}
  - id: anthropic-api
    model_name: sonnet
    kind: api
    provider: anthropic
    model: claude-sonnet-5-5
    credential_ref: env:ANTHROPIC_API_KEY
    tags: [coding, review]
    budget: {type: usd, limit_amount: 30, window: monthly}
  - id: deepseek-api
    model_name: deepseek
    kind: api
    provider: deepseek
    model: deepseek-chat
    credential_ref: keyring:spillway:deepseek
    enabled: false                  # 省略 = true
    tags: [coding, cheap]
    budget: {type: usd, limit_amount: 10, window: monthly}
  - id: qwen-local
    model_name: qwen-coder
    kind: local
    provider: ollama
    model: qwen3-coder:30b
    params: {api_base: http://127.0.0.1:11434}
    tags: [coding, private-ok]

groups:
  coding: {all: [coding]}

routes:
  review:
    target: {group: coding}
    mode: task
    tags: [daily]
    record_context: true            # 省略 = 沿用全域
    budget_precheck: true           # 啟用預估額度檢查（4.2.5）
    targets:                       # 外層 = slot，內層 = position
      - [{deployment: claude-sub, effort: high}]
      - [{deployment: anthropic-api, effort: medium},
         {deployment: deepseek-api, effort: medium, weight: high}]
      - [{deployment: qwen-local}]
  quick:
    target: {tags: {all: [coding], none: [review]}}
```

## 4 路由規則

使用者拖曳設定的 slot 順序優先於預設層級順序；同一 slot 內的雲端成員依優先權重負載平衡。

![路由決策流程](images/routing-flow.png)

### 4.1 請求解析

4.1.1 `model` 以 `local/` 開頭時，系統必須直接呼叫其後指定的 local 成員（以 id 或 `model_name` 比對），不可經過路由與 fallback。

4.1.2 `model` 等於 route 名稱（可帶 `route:` 前綴）時，必須使用該 route。

4.1.3 `model` 等於成員的 `model_name` 時，系統必須以全部同名成員組成隱含 route（mode 為 `chat`，採預設順序）。

4.1.4 皆不符時必須回傳 HTTP 404，錯誤碼 `route_not_found`。

### 4.2 候選篩選

4.2.1 成員必須 `enabled = 1`。

4.2.2 估算 token 數（字元數 ÷ 4）不可超過 `capabilities.ctx`；請求含 tools 時 `capabilities.tools` 必須為 true；含圖片時 `capabilities.vision` 必須為 true。

4.2.3 `chat` route 不可選用 `cli` 成員；請求帶 `metadata.mode = task` 時視同 `task`。

4.2.4 `cli` 與 `api` 成員必須剩餘額度大於 0 且不在冷卻期（第 5 節）；`local` 成員不檢查額度，改檢查健康狀態（端點 2 秒內回應）。`cli` 成員另須進行中的子行程數小於 `params.max_concurrency`（未設定時為 1）；達上限者本次視為不可用（排除原因 `concurrency_full`），不排隊、不寫入 `usage_ledger`，由下一個成員接手（NFR-6）。

4.2.5 Route 勾選預估額度檢查（`budget_precheck = 1`）時，`cli` 與 `api` 成員的 `est_cost_usd_p90`（5.4）不可超過該成員剩餘額度；超過者本次跳過，ledger 記為 `skipped_precheck`，不設冷卻。

4.2.6 Route 未勾選（`budget_precheck = 0`，預設）時不做預估檢查：成員持續被選用，直到上游回報額度用完或本地計算剩餘為 0；之後依 5.3.2 冷卻至下一個可用時間，到期自動恢復為候選，不需人工操作。

### 4.3 順序決定

4.3.1 順序來源的優先序（高至低）：① 使用者於 UI 拖曳設定並儲存於 `route_targets` 的 slot 順序；② 預設層級順序。

4.3.2 預設層級順序：slot 1 為全部 `cli` 成員，slot 2 為全部 `api` 成員，每個 `local` 成員各佔一個 slot，依 id 字母序接在後面；優先權重預設 `medium`。

4.3.3 Route 已有手動排序，而目標新增未出現於 `route_targets` 的成員時，新成員必須依 4.3.2 的層級接在手動 slot 之後，不可插入手動 slot 之間；UI 必須標示為「未排序」。

4.3.4 使用者「還原預設」時，系統必須刪除該 route 的全部 `route_targets`，回到 4.3.2。

### 4.4 Slot 內選擇

4.4.1 Slot 內先選雲端成員（`cli`、`api`），以加權隨機選一個：權重 = 優先權重值 × 剩餘額度比例。

| 優先權重 | 權重值 |
| --- | --- |
| high | 3 |
| medium | 2 |
| low | 1 |

4.4.2 剩餘額度比例為 0–1；額度類型 `none` 的成員以 1 計。

4.4.3 被選中的成員失敗後，必須移出本次候選，再於同 slot 剩餘雲端成員中重新加權選擇。

4.4.4 雲端成員用盡後，依 `position` 依序嘗試同 slot 的 `local` 成員。

4.4.5 Slot 內成員全部失敗或不可用時，進入下一個 slot。

### 4.5 失敗處理

4.5.1 每次嘗試必須寫入 `usage_ledger` 一列（含 `slot`、`fallback_depth`、`latency_ms`）。

4.5.2 上游結果與處置：

| 上游結果 | ledger status | 處置 |
| --- | --- | --- |
| 成功 | `ok` | 回應客戶端 |
| 額度用完（見 5.3） | `quota_exhausted` | `cooldowns.exhausted_until` 設為下一個重置點，`reason` 為 `quota_exhausted` |
| 限流（HTTP 429） | `rate_limited` | `cooldowns.exhausted_until` 設為現在 + 60 秒，`reason` 為 `rate_limited` |
| 其他錯誤或逾時 | `error` | 換下一個成員 |

冷卻寫入 `cooldowns`（每個成員至多一列）；已有較晚的冷卻期限時保留原列。設定冷卻不可建立或修改 `budgets` 列。

4.5.3 全部 slot 皆失敗時必須回傳 HTTP 503，錯誤碼 `no_candidate`，body 列出每次嘗試的成員與狀態，`error.message` 形狀為 `沒有可用的候選成員;嘗試:<成員>:<狀態>, …;被篩:<成員>:<原因>, …`（分號、冒號與逗號為 ASCII 字元；無嘗試時為 `嘗試:無`；無被篩項時省略該段）。`<狀態>` 為 `usage_ledger.status` 的值、`disabled` 或 `adapter_unavailable`；`<原因>` 為 `disabled`、`mode_chat_excludes_cli`、`ctx_exceeded`、`tools_unsupported`、`vision_unsupported`、`adapter_unavailable`、`cooling`、`concurrency_full`、`quota_exhausted`、`unhealthy`、`skipped_precheck`、`shutting_down` 之一。

## 5 額度計算與上游訊號

剩餘額度 = 上限 − 本期帳本 `cost_usd` 加總；上游明確回報用完時，以上游為準。

### 5.1 計算期間

| window | 本期起點 | 下一個重置點 |
| --- | --- | --- |
| `daily` | 當日 00:00 UTC | 次日 00:00 UTC |
| `weekly` | 本週一 00:00 UTC | 下週一 00:00 UTC |
| `monthly` | 本月 `reset_anchor` 日 00:00 UTC（尚未到則取上月） | 下月同日 |
| `none` | 無起點（累計） | 現在 + 1 小時（用於重新探測） |

### 5.2 各類型的花費來源

5.2.1 `api`：必須使用 LiteLLM `completion_cost()` 的結果寫入 `cost_usd`；無定價的模型寫入 NULL 並於 UI 標示「未計價」。（LiteLLM 為 Python 版專屬，Rust 版依 15.2 「花費與 token 估算」，該列定義前依 15.8.3 只比型別）

5.2.2 `cli`（claude-cli）：必須使用 `claude -p --output-format json` 回應中的 `total_cost_usd` 寫入 `cost_usd`，扣抵 `credit_pool` 類型的額度。

5.2.3 `cli`（codex-cli）：輸出不含金額時寫入 token 數，`cost_usd` 為 NULL；額度類型應設為 `none`，可用性只依 5.3 的上游訊號判斷。

5.2.4 `local`：`cost_usd` 寫入 0。

### 5.3 上游訊號判定

5.3.1 判定規則必須存於成員 `params.signals`，可於 UI 修改；未設定時使用模組 manifest 的 `default_signals`（7.6.2）。下表為內建模組的預設值。

| 成員 | 額度用完 | 限流 |
| --- | --- | --- |
| claude-cli | JSON `is_error = true` 且訊息符合 `credit\|usage limit\|extra usage` | 訊息符合 `rate limit\|overloaded` |
| codex-cli | 結束碼 ≠ 0 且 stderr 符合 `usage limit\|quota` | stderr 符合 `rate limit\|429` |
| api | HTTP 402，或錯誤碼 `insufficient_quota` | HTTP 429 |
| api（anthropic） | 同上，另加：HTTP 400 且訊息符合 `credit balance` | 同上，另加：HTTP 529 |

5.3.2 判定為額度用完時，系統必須將該成員 `cooldowns.exhausted_until` 設為 5.1 的下一個重置點（`reason` 為 `quota_exhausted`），即使本地計算仍有剩餘；成員沒有 `budgets` 列時以 window `none` 計。

5.3.3 使用者可於 UI 手動清除冷卻，即刪除該成員的 `cooldowns` 列。

### 5.4 Token 與花費預估

5.4.1 每次嘗試呼叫前，系統必須計算並寫入 `est_tokens_in`、`est_tokens_out_p50`、`est_tokens_out_p90`、`est_cost_usd_p90`。

5.4.2 輸入 token：以 `litellm.token_counter()` 依成員模型計算；CLI 成員另加 `params.cli_overhead_tokens`（初始 0，依帳本的預估與實際差異調整）。（`litellm.token_counter()` 為 Python 版專屬，Rust 版依 15.2 「花費與 token 估算」，該列定義前依 15.8.3 只比型別）

5.4.3 輸出 token：請求帶 `max_tokens` 時，P50 與 P90 皆取 `max_tokens`；否則取同一 route 與成員最近 50 筆 `ok` 紀錄之 `tokens_out` 的 P50 與 P90；不足 5 筆時 P50 取 500、P90 取 2,000。

5.4.4 花費：以 LiteLLM 價格表計算；無定價的模型與 codex-cli 寫入 NULL，且不參與 4.2.5 的檢查。（LiteLLM 的計算方式為 Python 版專屬，Rust 版依 15.2 「花費與 token 估算」，該列定義前依 15.8.3 只比型別）

5.4.5 預估值必須出現於：dry-run 結果、帳本、`/v1/chat/completions` 回應 header `X-Spillway-Est-Cost`、MCP `ask` 與 `quota_status` 的輸出。

5.4.6 總覽的「預估可用天數」= 剩餘額度 ÷ 最近 7 天日均實際花費；最近 7 天無花費時顯示「—」。

## 6 Effort 對應

系統內部只使用五個等級；各 adapter 負責轉換成上游參數。

### 6.1 來源優先序

6.1.1 Effort 必須依下列順序取第一個非空值：① `route_targets.effort`；② 請求的 `reasoning_effort`；③ 不傳（上游預設）。

### 6.2 降級

6.2.1 成員 `capabilities.efforts` 不含所需等級時，必須往下降至最近的支援等級；無任何較低等級時不傳。

6.2.2 實際使用的等級必須寫入 `usage_ledger.effort`。

### 6.3 預設對應表

各內部等級對應的上游參數由模組 manifest 的 `effort_map` 宣告（7.6.2）；下表為內建模組的宣告值。

| 內部等級 | anthropic_api、openai_api、litellm 模組 | codex-cli | claude-cli |
| --- | --- | --- | --- |
| `none` | 不傳 `reasoning_effort` | `minimal` | 依 `params.effort_args` |
| `low` | `low` | `low` | 依 `params.effort_args` |
| `medium` | `medium` | `medium` | 依 `params.effort_args` |
| `high` | `high` | `high` | 依 `params.effort_args` |
| `max` | `max`（模型不支援時依 6.2 降級） | `xhigh` | 依 `params.effort_args` |

6.3.1 codex-cli 必須以 `-c model_reasoning_effort=<值>` 傳遞。

6.3.2 claude-cli 的參數必須由 `params.effort_args`（字串模板清單，例如 `["--effort", "{effort}"]`）決定；未設定時不傳。模組 manifest 的 `default_params.effort_args` 視為已設定（claude_cli 模組為 `["--effort", "{effort}"]`），因此只有成員與模組皆未設定、或設為空清單時不傳；內部等級 `none` 一律不傳。實作前需以當時安裝的 CLI 版本確認參數名稱。

## 7 Adapter 規格

每種 AI 的每種接入方式以一個 adapter 模組實作同一介面（7.6），路由器不可依賴任何 adapter 的內部細節。

### 7.1 介面

下列 Python 介面與 7.1.1、7.1.2 的模組形式為 Python 版專屬（兩個實作共同的擴充形式見 15.11）；Rust 版不必實作相同的語言介面，對外行為依第 15 節，送往上游的請求依 15.3；7.1.1 中未宣告 `provider` 的處理，Rust 版由內建協定 adapter 移植定義。

```python
class Adapter(Protocol):
    async def call(self, dep: Deployment, req: ChatRequest, effort: str | None) -> CallResult: ...
    async def health(self, dep: Deployment) -> bool: ...

@dataclass
class CallResult:
    ok: bool
    text: str
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    status: str          # ok | error | quota_exhausted | rate_limited
    error: str | None
    tool_calls: list[dict] | None = None   # 上游回傳的工具呼叫（OpenAI 格式）；沒有時為 None
    finish_reason: str | None = None
```

7.1.1 系統啟動時必須自動發現全部 adapter 模組，並依 manifest 的 `providers` 註冊；沒有任何模組宣告的 `provider`，依 `kind` 使用通用 `litellm` 模組（`api`、`local`）。

7.1.2 新增一種 AI 或一種接入方式，只可新增一個 adapter 模組資料夾，不可修改路由器或任何既有檔案。

### 7.2 LiteLLM 呼叫（anthropic_api、openai_api、litellm 模組）

LiteLLM 呼叫為上述三個模組共用的函式，不是獨立模組；7.2.1–7.2.3 適用於使用它的模組。7.2.1 的模型字串形式與本節的 LiteLLM 呼叫方式為 Python 版專屬，Rust 版送往上游的請求依 15.3；`params.api_base` 的採用、7.2.2、7.2.3 的行為要求兩個實作皆適用（Rust 版適用於對應的內建協定 adapter）；LiteLLM 升版依 15.1.3。

7.2.1 模型字串必須為 `<provider>/<model>`；`params.api_base` 存在時傳入。

7.2.2 憑證必須於呼叫當下依 `credential_ref` 解析，不可寫入日誌。

7.2.3 逾時預設 120 秒，可由 `params.timeout` 覆寫。

### 7.3 claude-cli adapter

7.3.1 命令：`claude -p --output-format json --model <model>` 加 `params.effort_args` 與 `params.extra_args`；prompt 必須經 stdin 傳入。

7.3.2 子行程環境以閘道的環境為底，必須移除下列變數，確保使用訂閱登入與 Agent SDK credit：`ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL`、`CLAUDE_CODE_USE_BEDROCK`、`CLAUDE_CODE_USE_VERTEX`、`CLAUDE_CODE_USE_FOUNDRY`，以及全部成員中 `credential_ref` 為 `env:<變數名>` 的變數；後者的值必須作為該次 CLI 輸出的遮罩對象（2.4）。官方 CLI 的登入變數照常繼承，系統不可讀取（1.1.2）。

7.3.3 子行程必須以不載入本閘道 MCP 的方式啟動（預設 `--strict-mcp-config`），並設定環境變數 `SPILLWAY_DEPTH=1`。

7.3.4 工作目錄必須為 `params.cwd`，未設定時使用閘道資料目錄下的空目錄 `work/claude`。

7.3.5 回應文字取 JSON `result`；token 取 `usage`；花費取 `total_cost_usd`。

7.3.6 逾時預設 600 秒。

### 7.4 codex-cli adapter

7.4.1 命令：`codex exec --json --skip-git-repo-check -m <model> -o <暫存檔>` 加 effort 參數與 `params.extra_args`，prompt 為最後一個引數。

7.4.2 回應文字取暫存檔內容；token 取 JSONL 事件中的 usage 欄位。

7.4.3 子行程必須設定 `SPILLWAY_DEPTH=1`，並固定以 `--ignore-user-config`（不讀 `$CODEX_HOME/config.toml`，登入仍用 `CODEX_HOME`）與 `-c mcp_servers={}` 停用全部 MCP（含本閘道）：`-c mcp_servers={}` 由模組附加於 `params.extra_args` 之後、prompt 前的 `--` 之前，為最後一個 `-c`；兩者 `params.extra_args` 皆不可移除或覆寫。`-c mcp_servers={}` 會與設定檔合併而非取代，因此必須同時不讀 config.toml。`params.extra_args` 含 `--`、位置引數、以 `-c`/`--config`（含 `-c=`、`-cKEY=`）設定 `mcp_servers`、`profile` 或 `features`，或含 `--enable`、`-p`/`--profile`（含 `-pNAME`、`-p=NAME`）時該次結果為 `error`。子行程環境必須移除 `OPENAI_API_KEY`、`CODEX_API_KEY`、`OPENAI_BASE_URL`，以及 7.3.2 所述的成員 `env:` 憑證變數。

### 7.5 Messages 轉為 CLI prompt

7.5.1 `system`、`user`、`assistant` 訊息必須依序串成純文字，每段以 `[System]`、`[User]`、`[Assistant]` 標頭開頭。

7.5.2 含 tools 或圖片的請求不可送往 CLI 成員（由 4.2.2 的 capabilities 篩除）。

### 7.6 Adapter 模組

7.6.1 每個模組必須是 `adapters/<module>/` 下的一個子套件，包含 `__init__.py`（匯出 `MANIFEST` 與 `create()`）、`adapter.py`、`models.yaml`、`README.md`。

7.6.2 `MANIFEST` 必須宣告下列欄位：

| 欄位 | 內容 |
| --- | --- |
| `providers` | 此模組處理的 `provider` 值 |
| `kinds` | 支援的 `kind` |
| `access` | `subscription-cli`、`api`、`local` |
| `cardinality` | `single` 或 `multiple` |
| `effort_map` | 五個內部等級各自對應的上游值；不傳時為 null |
| `default_signals` | 額度用完與限流的判定規則（5.3.1） |
| `default_capabilities` | 能力預設 |
| `default_params` | 參數預設，例如 `timeout`、`effort_args` |
| `requires` | 需要的執行檔或環境變數 |
| `contract_version` | 模組契約版本，第一版為 1 |

7.6.3 模組只可引用共用契約（Adapter 介面、`CallResult`、`AdapterManifest`、子行程執行器、LiteLLM 呼叫），不可引用路由、資料庫、HTTP 或 MCP 程式。

7.6.4 可用模型清單：`models.yaml` 每筆必須含 `id`、`label`、`capabilities`（`ctx`、`tools`、`vision`、`efforts`）；`efforts` 只可含 6 節的五個等級。模組可另提供 `list_models()` 於執行期查詢上游，結果與 `models.yaml` 合併，同 `id` 以 `models.yaml` 的能力為準；查詢失敗時只回傳 `models.yaml` 並附原因。可用模型清單不寫入資料庫，也不寫入匯出檔。

7.6.5 Cardinality：`provider` 屬於 `single` 模組且已有 1 個成員時，建立或匯入第二個成員必須整筆失敗，回傳 HTTP 409，錯誤碼 `provider_single`。

7.6.6 同一 `provider` 被兩個模組宣告時，系統必須拒絕啟動並列出兩個模組。

7.6.7 模組載入失敗（無法 import、manifest 不合法、`contract_version` 不支援）時，該模組不註冊；使用其 `provider` 的成員於候選篩選時視為不可用（原因 `adapter_unavailable`），其他模組照常運作。

7.6.8 每個模組必須通過共用合約測試：manifest 欄位完整且合法、`effort_map` 五個等級皆有定義、`models.yaml` 可解析，以及 `call` 對成功、額度用完、限流、錯誤四種上游輸出回傳對應的 `status`。

7.6.9 內建模組：

| 模組 | provider | access | cardinality |
| --- | --- | --- | --- |
| `claude_cli` | `claude-cli` | `subscription-cli` | `single` |
| `codex_cli` | `codex-cli` | `subscription-cli` | `single` |
| `anthropic_api` | `anthropic` | `api` | `multiple` |
| `openai_api` | `openai` | `api` | `multiple` |
| `litellm` | 其他未被宣告的 `api`、`local` provider | `api`、`local` | `multiple` |

## 8 MCP 介面與設定輸出

每個群組對應一個 MCP 端點，只暴露該群組的 route；系統依群組與工具種類產生可直接套用的設定。

### 8.1 MCP server

8.1.1 傳輸：Streamable HTTP（有 session），端點 `http://127.0.0.1:<port>/mcp/<group>`；`/mcp/all` 暴露全部 route。`<group>` 不是既有群組且不是 `all` 時，開新 session 的請求（不帶 `Mcp-Session-Id`）必須回傳 HTTP 404，錯誤碼 `not_found`。群組名 `all` 為保留字（9.2.5）。

8.1.2 不支援 HTTP 的工具必須使用 stdio 橋接命令 `spillway mcp-stdio --group <group> [--port 8787]`，橋接將請求轉發至 `http://127.0.0.1:<port>/mcp/<group>`（主機固定，不提供 `--host`）；token 取自環境變數 `SPILLWAY_TOKEN`，先去除前後空白（含結尾換行）。橋接不開啟資料庫、不經系統代理。結束碼：

| 結束碼 | 情況 |
| --- | --- |
| 0 | stdin 結束，且進行中的請求皆已回應 |
| 1 | 閘道未啟動；不是閘道拒絕的 HTTP 錯誤（含 MCP SDK 傳輸層的 503「Too many open sessions」、404「Session not found」、body 不是 9.1.2 格式或錯誤碼與狀態碼不成對）；GET 通知串流被拒（例如停機時重連）；其他傳輸錯誤、閘道連線意外結束、stdin 結束後 5 秒內未收到回應 |
| 2 | `SPILLWAY_TOKEN` 未設定、去除前後空白後為空或含 CR/LF，或 `SPILLWAY_DEPTH` 含 CR/LF；不連線 |
| 3 | 被閘道拒絕：POST 的回應為下方成對的狀態碼與 9.1.2 錯誤碼 |
| 130 | Ctrl-C（SIGINT） |

橋接只檢查 POST 且狀態碼 ≥ 400 的回應（2xx 不讀 body；3xx 交給 MCP SDK 的同源轉址；GET 與 DELETE 由 SDK 處理）。狀態碼為 401、403、404、503 時讀取 body（上限 64 KiB，`Content-Type` 必須為 `application/json`）；body 為 9.1.2 格式（`error.code` 為字串）且錯誤碼與狀態碼成對——401 `unauthorized`；403 `recursion_blocked`、`host_not_allowed` 或 `origin_not_allowed`；404 `not_found`；503 `shutting_down`——才視為被閘道拒絕：橋接必須對每個尚未回應的 request 寫出 JSON-RPC 錯誤 `{"code": -32000, "message": "<錯誤碼>: rejected by gateway (HTTP <狀態碼>)", "data": {"code": "<錯誤碼>", "http_status": <狀態碼>}}`，結束碼 3。其他 HTTP 錯誤寫出 `{"code": -32000, "message": "gateway error (HTTP <狀態碼>)", "data": {"http_status": <狀態碼>}}`（不帶 `data.code`），結束碼 1；閘道未啟動與其他傳輸錯誤的 JSON-RPC 錯誤不帶 `data`。橋接不轉印對方回應的 `message`。

8.1.3 驗證：請求必須帶 `Authorization: Bearer <token>`；token 於首次設定時產生，`settings` 只存其 SHA-256 雜湊。適用於 `/mcp/*` 的 POST、GET、DELETE；scheme 比對不分大小寫；token 雜湊每次請求讀取，rotate 立即生效（舊 token 下一次請求即被拒）。比對前先去除 header 值與 token 的前後空白（含結尾換行）。缺少 header、格式錯誤、雜湊不符或尚未產生 token 時回傳 HTTP 401，錯誤碼 `unauthorized`，並帶 `WWW-Authenticate: Bearer`（不帶 `resource_metadata`）。

8.1.4 群組端點暴露的 route：`target.group` 等於該群組名稱者。Session 綁定建立它的群組：以同一 session id 改打別的群組時回傳 HTTP 404，body 為 MCP SDK 的 JSON-RPC 格式（例外於 9.1.2）。群組在 session 期間被刪除時，該 session 的 `tools/list` 回空清單、`ask` 回 `route_not_found`。

8.1.5 Route 或群組變更後，系統必須發送 `notifications/tools/list_changed`；initialize 宣告 `tools.listChanged = true`。通知送至所有列過或呼叫過工具的 session（不區分群組），但只送達已開啟 GET 通知串流的 session；未開啟者在下一次 `tools/list` 取得新清單。

8.1.6 遞迴防護：請求帶 `X-Spillway-Depth` 且值 ≥ 1 時必須拒絕，HTTP 403，錯誤碼 `recursion_blocked`；在驗證 token 之前判斷；任一 `X-Spillway-Depth` 值不符 `^[0-9]+$`（含空字串、`-1`、`+1`）或 ≥ 1 即拒絕。stdio 橋接必須將環境變數 `SPILLWAY_DEPTH` 轉為此 header（值為空或未設定時不送）。

8.1.7 `/mcp/*` 請求的檢查順序：Host／Origin（9.1.4）→ 遞迴防護（8.1.6）→ Bearer（8.1.3）→ 停機中或尚未啟動完成回傳 HTTP 503，錯誤碼 `shutting_down` → 群組存在（8.1.1）。以上錯誤皆為 9.1.2 格式。MCP SDK 傳輸層產生的錯誤（400 Content-Type 或 session id 缺漏、404 session 不存在、406、413、503 session 數達上限 100）body 為 JSON-RPC 或純文字，例外於 9.1.2。

| Tool | 輸入 | 輸出（`structuredContent`） |
| --- | --- | --- |
| `ask` | `route`（enum，該群組的 route；群組無 route 時為不設 enum 的字串，說明列「目前無 route」）、`prompt`（非空字串）、`effort`（選填，enum 五級）；不接受其他鍵 | `{text, served_by, slot, cost_usd, tokens_in, tokens_out}`；`content` 為一段回應文字。不露出預估值 |
| `list_routes` | 無 | `{"routes": [{name, mode, ordered, slots: [{slot, members: [{id, effort, weight}]}]}]}`；`ordered` 為 `manual`（有已儲存的手動排序）或 `default`；兩者皆與 Router 實際使用的順序相同（`OrderPlanner.plan`）：已儲存的 slot 在前，未列入的 route 成員依預設順序（4.3.2）接為後面的層級，已不屬 route 目標的成員略過，chat route 不列 cli 成員 |
| `quota_status` | 無 | `{"members": [{id, kind, provider, budget_type, remaining, window, cooling_until, cooling_reason}]}`；範圍為群組選擇器涵蓋的成員（與 `GET /api/groups/{name}` 的成員預覽同源；`all` 為全部成員），依 id 排序；推導與 `/api/dashboard` 相同 |

8.1.8 `ask` 以 route 名稱解析（不會退回同名成員的隱含 route），帳本與 `/v1` 同一路徑寫入。三個工具的 outputSchema 皆為 `type: object`、`required: []`，成功與錯誤結果都相容。

8.1.9 工具錯誤結果：一律為 `CallToolResult`，`isError = true`，`content` 為一段 JSON 文字，`structuredContent` 為 `{"error": {"code": "<錯誤碼>", "message": "<說明>"}, "attempts": [...]}`（不是 JSON-RPC error）。錯誤碼：`route_not_found`（route 不存在，或 session 綁定的群組已被刪除）、`no_candidate`（帶 `attempts`，每項 `{deployment, status, slot}`）、`validation_error`（route 不是本群組可見的 route、prompt 為空、effort 不在五級內或多餘參數）、`shutting_down`（停機期滿，進行中的 `ask` 已取消）、`internal_error`（非預期錯誤，訊息只含例外型別）。

8.1.10 `list_routes` 一律與 Router 使用同一個規劃器（`OrderPlanner.plan`）；與 `GET /api/routes/{name}` 的 `order`（只含已儲存的 slot）不同，後者不含接在後面的未列入成員。

### 8.2 設定輸出

8.2.1 介面：`GET /api/mcp/config?group=<group>&tool=<tool>`。`tool` 為 `claude-code`、`codex`、`cursor`、`gemini`、`stdio` 之一，其他值回傳 HTTP 422，錯誤碼 `validation_error`；`group` 不存在且不是 `all` 時回傳 HTTP 404，錯誤碼 `not_found`。回應：

```json
{
  "group": "coding",
  "tool": "codex",
  "variants": [
    {"label": "~/.codex/config.toml", "scope": "user", "format": "toml",
     "path": "~/.codex/config.toml", "filename": "config.toml", "content": "…"},
    {"label": "codex mcp add 命令", "scope": "user", "format": "command",
     "path": null, "filename": null, "content": "…"}
  ],
  "env": ["SPILLWAY_TOKEN"],
  "notes": [{"severity": "info", "text": "…"}]
}
```

`variants` 為可套用的放置方式，第一項為建議做法；`scope` 為 `user` 或 `project`，`format` 為 `command`、`json` 或 `toml`；命令類的 `path`、`filename` 為 null。回應沒有頂層 `format`、`path`、`content`，一律讀 `variants`。`notes` 的 `severity` 為 `info` 或 `warn`。

8.2.2 設定內容一律以環境變數 `SPILLWAY_TOKEN` 參照 token，不輸出明文（資料庫只存雜湊，閘道無法事後取得明文）。

8.2.3 模板檔為 `templates/mcp/<tool>.<kind>.j2`，由程式內的 `TOOL_FORMATS` 對應；同一模板可由多個 variant 共用（`cursor.http.j2` 同時供 Cursor 的 user 與 project 兩個 variant；`stdio` 工具沿用各工具既有的 `*.stdio.j2`）；新增工具只新增模板檔與 `TOOL_FORMATS` 一行。模板變數為 `name`（`spillway-<group>`）、`url`、`group`、`port`（serve 綁定埠）、`command`、`args_prefix`；群組名依格式跳脫（JSON 字串、TOML 字串與鍵、shell 單引號、URL 路徑段編碼）。stdio 的 `command` 為此台機器上 `spillway` 的絕對路徑（找不到時為 Python 直譯器加 `-m spillway`），模板不含 `SPILLWAY_DEPTH`。

8.2.4 各工具的欄位名稱以實作當時該工具的官方文件為準。下表與範例為實作（CHG-20261102-01）的實際輸出。

| 工具（`tool`） | `variants`（依序） | 放置位置 | `notes` |
| --- | --- | --- | --- |
| Claude Code（`claude-code`） | ① 專案 `.mcp.json`（`type: http`）② `claude mcp add --transport http` 命令（header 以單引號傳入） | 專案根目錄 `.mcp.json`；命令 | warn：local／user scope 是否在執行期展開 `${SPILLWAY_TOKEN}` 官方文件未載明，以 `claude mcp get` 確認顯示字面值 |
| Codex（`codex`） | ① `[mcp_servers.<name>]`（`bearer_token_env_var`、`tool_timeout_sec = 600`）② `codex mcp add` 命令 | `~/.codex/config.toml`；命令 | info：先設定 `SPILLWAY_TOKEN`；`tool_timeout_sec` 預設 60 秒，此處設為 600 |
| Cursor（`cursor`） | ① `mcpServers` JSON ② 同內容，專案層級 | `~/.cursor/mcp.json`；專案 `.cursor/mcp.json` | info：先在啟動 Cursor 的環境設定 `SPILLWAY_TOKEN` |
| Gemini CLI（`gemini`） | ① `mcpServers` JSON（`httpUrl`）② `mcpServers` JSON（stdio，`mcp-stdio`） | `~/.gemini/settings.json` | warn：若 401 或 `gemini mcp list` 顯示未連線，改用 variants[1]；info：stdio 的 `command` 為此台機器的絕對路徑 |
| 通用 stdio（`stdio`） | ① Claude Code 專案 `.mcp.json`（`type: stdio`）② Cursor ③ Codex ④ Gemini CLI | 各工具的設定檔 | info：`command` 為此台機器的絕對路徑，換機器或重裝後須重新產生 |

各工具的 stdio variant 以各自的寫法參照環境變數：Claude Code `${SPILLWAY_TOKEN}`、Cursor `${env:SPILLWAY_TOKEN}`、Codex `env_vars = ["SPILLWAY_TOKEN"]`、Gemini CLI `$SPILLWAY_TOKEN`。

群組 `coding`、埠號 8787 的輸出範例（`spillway` 位於 `/usr/local/bin/spillway` 時）：

`claude-code variants[0]`（.mcp.json）：

```json
{
  "mcpServers": {
    "spillway-coding": {
      "type": "http",
      "url": "http://127.0.0.1:8787/mcp/coding",
      "headers": {
        "Authorization": "Bearer ${SPILLWAY_TOKEN}"
      }
    }
  }
}
```

`claude-code variants[1]`：

```bash
claude mcp add --transport http spillway-coding http://127.0.0.1:8787/mcp/coding --header 'Authorization: Bearer ${SPILLWAY_TOKEN}'
```

`codex variants[0]`（~/.codex/config.toml）：

```toml
[mcp_servers.spillway-coding]
url = "http://127.0.0.1:8787/mcp/coding"
bearer_token_env_var = "SPILLWAY_TOKEN"
tool_timeout_sec = 600
```

`codex variants[1]`：

```bash
codex mcp add spillway-coding --url http://127.0.0.1:8787/mcp/coding --bearer-token-env-var SPILLWAY_TOKEN
```

`cursor variants[0]`、`variants[1]`（~/.cursor/mcp.json、.cursor/mcp.json）：

```json
{
  "mcpServers": {
    "spillway-coding": {
      "url": "http://127.0.0.1:8787/mcp/coding",
      "headers": {
        "Authorization": "Bearer ${env:SPILLWAY_TOKEN}"
      }
    }
  }
}
```

`gemini variants[0]`（~/.gemini/settings.json）：

```json
{
  "mcpServers": {
    "spillway-coding": {
      "httpUrl": "http://127.0.0.1:8787/mcp/coding",
      "headers": {
        "Authorization": "Bearer $SPILLWAY_TOKEN"
      }
    }
  }
}
```

`gemini variants[1]`、`stdio variants[3]`（~/.gemini/settings.json）：

```json
{
  "mcpServers": {
    "spillway-coding": {
      "command": "/usr/local/bin/spillway",
      "args": ["mcp-stdio", "--group", "coding", "--port", "8787"],
      "env": {
        "SPILLWAY_TOKEN": "$SPILLWAY_TOKEN"
      }
    }
  }
}
```

`stdio variants[0]`（Claude Code .mcp.json）：

```json
{
  "mcpServers": {
    "spillway-coding": {
      "type": "stdio",
      "command": "/usr/local/bin/spillway",
      "args": ["mcp-stdio", "--group", "coding", "--port", "8787"],
      "env": {
        "SPILLWAY_TOKEN": "${SPILLWAY_TOKEN}"
      }
    }
  }
}
```

`stdio variants[1]`（Cursor ~/.cursor/mcp.json）：

```json
{
  "mcpServers": {
    "spillway-coding": {
      "command": "/usr/local/bin/spillway",
      "args": ["mcp-stdio", "--group", "coding", "--port", "8787"],
      "env": {
        "SPILLWAY_TOKEN": "${env:SPILLWAY_TOKEN}"
      }
    }
  }
}
```

`stdio variants[2]`（Codex ~/.codex/config.toml）：

```toml
[mcp_servers.spillway-coding]
command = "/usr/local/bin/spillway"
args = ["mcp-stdio", "--group", "coding", "--port", "8787"]
env_vars = ["SPILLWAY_TOKEN"]
```

每段程式碼區塊即該 variant 的 `content` 全文。

## 9 HTTP API

對外呼叫採 OpenAI 相容格式；管理介面以 `/api` 為前綴，由 Web UI 使用。

### 9.1 通用規則

9.1.1 預設只綁 127.0.0.1；綁定其他位址時，所有端點必須驗證 Bearer token。

9.1.2 錯誤回應格式：`{"error": {"code": "<錯誤碼>", "message": "<說明>"}}`。請求驗證失敗（HTTP 422）的 `message` 為 `<欄位路徑>: <原因>`，多個錯誤以 `; ` 串接；欄位路徑以 `.` 連接各層（例如 `body.messages.0.role`）。設定匯入（3.1）的欄位路徑形式依 3.1.9；沒有對應欄位的驗證錯誤，`message` 可只有 `<原因>`。

9.1.3 `/v1/chat/completions` 回應必須帶 header `X-Spillway-Served-By`（成員 id）與 `X-Spillway-Slot`。

9.1.4 Host／Origin 檢查（全部端點，含 `/v1`、`/api`、`/mcp`；先於路由執行）：

- 每個請求必須剛好帶一個 `Host`，且完整符合 `^(127\.0\.0\.1|localhost)(:[0-9]{1,5})?$`（不分大小寫；埠號不比對）；否則回傳 HTTP 403，錯誤碼 `host_not_allowed`。`[::1]`、`localhost.`、`evil.localhost`、空值、HTTP/1.0 無 `Host` 皆拒絕。
- 重複的 `Host`：經 uvicorn（h11）到達的請求由伺服器在進入應用程式前以 HTTP 400 拒絕（非 9.1.2 格式）；直接送進 ASGI 應用程式的重複 `Host` 由 middleware 回傳 HTTP 403，錯誤碼 `host_not_allowed`（縱深防禦）。
- 請求帶 `Origin` 時，必須剛好一個，且完整等於（不分大小寫）`http://127.0.0.1:<port>` 或 `http://localhost:<port>`（`<port>` 為 serve 綁定埠）；結尾斜線、`null`、其他埠皆不符，回傳 HTTP 403，錯誤碼 `origin_not_allowed`。沒有 `Origin` 的請求（CLI、SDK）一律放行。
- 錯誤格式依 9.1.2；websocket 請求被拒時以 close code 1008 關閉。
- 不變式：所有 GET 端點不得改變狀態。

### 9.2 端點

| 方法 | 路徑 | 用途 |
| --- | --- | --- |
| POST | `/v1/chat/completions` | 呼叫；`model` 依 4.1 解析 |
| GET | `/v1/models` | 列出 route、`model_name`、`local/<id>` |
| GET | `/api/dashboard` | 各成員額度、本期花費、冷卻狀態、近 24 小時請求數、近 7 天日均花費（9.2.8） |
| GET / POST / PUT / DELETE | `/api/deployments[/{id}]` | 成員 CRUD（含 tag、額度） |
| POST | `/api/deployments/{id}/test` | 送出一次測試呼叫並回傳結果 |
| POST | `/api/deployments/{id}/clear-cooldown` | 清除冷卻（刪除該成員的 `cooldowns` 列） |
| GET / POST / PUT / DELETE | `/api/groups[/{name}]` | 群組 CRUD；GET 含符合的成員預覽；建立時的名稱規則（含保留字 `all`）見 9.2.5 |
| POST | `/api/groups/preview` | 以尚未儲存的選擇器預覽符合的成員，不寫入（9.2.8） |
| GET / POST / PUT / DELETE | `/api/routes[/{name}]` | Route CRUD |
| GET | `/api/routes/{name}/effective-order` | 目前生效的靜態順序（9.2.8） |
| PUT | `/api/routes/{name}/order` | 儲存拖曳結果：body 為 slot 清單，每個 slot 含成員 id、effort、優先權重 |
| DELETE | `/api/routes/{name}/order` | 還原預設順序（4.3.4） |
| POST | `/api/routes/{name}/dry-run` | 以範例請求模擬路由，回傳候選與順序，不呼叫上游 |
| GET | `/api/ledger` | 帳本查詢；參數 `from`、`to`、`deployment`、`route`、`status`、`min_depth`（9.2.8）、`page` |
| GET | `/api/ledger/export.csv` | 帳本 CSV 匯出（9.2.6）；篩選參數同 `/api/ledger`，不分頁 |
| GET | `/api/mcp/config` | MCP 設定輸出（8.2）；參數 `group`（群組名或 `all`）、`tool`（`claude-code`、`codex`、`cursor`、`gemini`、`stdio`） |
| GET | `/api/mcp/catalog` | UI-8 的資料（9.2.8）；參數 `group`（群組名或 `all`，預設 `all`） |
| POST | `/api/config/import` | 匯入 YAML（3.1.7–3.1.13）；參數 `mode=merge\|replace`（必填）、`dry_run=true`（選填，預覽後回滾）；`replace` 先檢查匯入前的設定可通過憑證鍵名掃描與解析，否則 422 `validation_error`（3.1.11） |
| GET | `/api/config/export` | 匯出 YAML（3.1.6） |
| GET | `/api/settings` | 設定總覽（9.2.7） |
| POST | `/api/settings/token/rotate` | 重新產生存取 token |
| POST / GET / DELETE | `/mcp/<group>` | MCP Streamable HTTP 端點（8.1；需 Bearer token） |

9.2.1 Context 與預估相關端點：

| 方法 | 路徑 | 用途 |
| --- | --- | --- |
| GET | `/api/ledger/{id}/payload` | 該次嘗試的請求與回應內容；已過保存期限回傳 410，錯誤碼 `payload_expired` |
| GET / PUT | `/api/settings/context` | `record_context_default`、`context_retention_days`、`context_max_bytes` |
| POST | `/api/estimate` | 以請求 body 回傳各候選成員的預估 token 與花費，不呼叫上游 |

9.2.2 Adapter 模組端點：

| 方法 | 路徑 | 用途 |
| --- | --- | --- |
| GET | `/api/adapters` | 列出已發現的模組：`providers`、`kinds`、`access`、`cardinality`、現有成員數、是否可用與原因、可用模型清單（7.6.4）；供 UI-2、UI-4 選單使用 |

9.2.3 `POST /api/deployments` 與 `POST /api/config/import` 違反 7.6.5 時回傳 HTTP 409，錯誤碼 `provider_single`。

9.2.4 `/api/deployments` 的成員物件與 `/api/dashboard` 的各成員必須帶頂層欄位 `cooling_until`（UTC ISO 8601 字串）與 `cooling_reason`（`quota_exhausted` 或 `rate_limited`）；兩者只在冷卻尚未到期時有值，到期後為 null（與 4.2.6「到期自動恢復」一致；`cooldowns` 列保留至下次設定或清除）。`budget` 物件不含冷卻欄位；`PUT` 帶 `budget: null` 只刪除額度設定，不影響冷卻。

9.2.5 群組名稱規則：建立或改名群組時（`POST /api/groups` 的 body 與設定匯入；目前沒有改名端點，`PUT /api/groups/{name}` 只取代選擇器），名稱必須為 1–200 個字元，不可為 `.`、`..` 或保留字 `all`（`/mcp/all` 暴露全部 route），不可含 `/` 或 Unicode 類別 Cc、Cf、Cs、Co、Cn、Zl、Zp、Zs 的字元（含空白、控制字元、雙向控制字元、孤立代理字元、私用字元），且必須已是 Unicode NFC 正規化形式（系統不自動正規化）；違反時回傳 HTTP 422，錯誤碼 `validation_error`。本規則不作用於 PUT、GET、DELETE 的路徑參數：既有名稱不符本規則的群組照常查詢、修改選擇器與刪除。成員 `id` 的規則不變。

9.2.6 帳本 CSV（`GET /api/ledger/export.csv`）：篩選參數與 `GET /api/ledger` 相同（`from`、`to`、`deployment`、`route`、`status`、`min_depth`），不分頁，依 `ts`、`id` 由新到舊；符合的列超過 100,000 列時回傳 HTTP 422，錯誤碼 `validation_error`（請縮小範圍）。欄位與順序：`id, ts, route_name, deployment_id, effort, tokens_in, tokens_out, cost_usd, status, slot, fallback_depth, latency_ms, est_tokens_in, est_tokens_out_p50, est_tokens_out_p90, est_cost_usd_p90, error`；第一列為標題；`ts` 為 UTC ISO 8601；null 輸出空字串；不含 `usage_payloads` 內容。字串欄位以 `=`、`+`、`-`、`@`、Tab、CR 開頭時，系統必須在值前加 `'`（試算表公式注入防護），數字欄不加。回應 `Content-Type: text/csv; charset=utf-8`，內容以 UTF-8 BOM 開頭、行尾 CRLF，並帶 `Content-Disposition: attachment; filename="spillway-ledger-<UTC yyyymmddThhmmssZ>.csv"`、`Cache-Control: no-store`、`X-Content-Type-Options: nosniff`。上限以串流前的計數判斷；計數與串流讀取之間新增的列可能使輸出在第 100,000 列處截掉最舊的列。下載期間系統持有該次讀取的快照。

9.2.7 設定總覽（`GET /api/settings`）：回應 `{"port", "data_dir", "version", "token_issued", "first_run", "counts": {"deployments", "groups", "routes", "ledger_ok"}, "context": {"record_context_default", "context_retention_days", "context_max_bytes", "payload_count", "payload_bytes"}}`。`port` 為 serve 綁定的埠（唯讀、顯示用，不含綁定位址）；`data_dir` 為資料庫所在目錄（與 `backups/` 同一目錄）；`version` 為已安裝的套件版本；`token_issued` 表示存取 token 是否已產生（回應不含 token 或其雜湊）；`first_run` 為 `counts.deployments == 0` 且 `counts.routes == 0`（不看群組；以 `replace` 匯入空設定後再次為 true）；`ledger_ok` 為 `status = ok` 的帳本列數；`payload_count`、`payload_bytes` 為 `usage_payloads` 的列數與 `bytes` 加總。

9.2.8 畫面用的讀取端點（CHG-20261103-01）：以下端點都不寫入，錯誤格式依 9.1.2，Host／Origin 檢查依 9.1.4。

- `POST /api/groups/preview`：body `{"selector": {"all", "any", "none"}}`（與 `PUT /api/groups/{name}` 相同；未知鍵或非字串 tag 回傳 HTTP 422，錯誤碼 `validation_error`）；回應 `{"members": [{"id", "model_name", "kind", "provider", "enabled"}]}`，形狀與 `GET /api/groups/{name}` 的 `members` 相同（含停用成員，依 `id` 排序）；空選擇器符合全部成員。
- `GET /api/routes/{name}/effective-order`：回應 `{"route", "mode", "ordered", "slots": [{"slot", "entries": [{"deployment", "effort", "weight", "unsorted"}]}]}`。`ordered` 為 `manual`（該 route 有手動排序）或 `default`。順序為靜態：候選為 route 目標的全部成員（含停用、冷卻中與健康不佳的成員，不經 4.2 的篩選），只有 chat route 排除 `cli` 成員；依 4.3 決定順序，`slot` 保留編號（可能不連號），不在手動排序內的成員接在後面並帶 `unsorted: true`；手動排序中已不屬於目標的成員不輸出。群組不存在或沒有成員時 `slots` 為 `[]`；route 不存在回傳 HTTP 404，錯誤碼 `not_found`。各 slot 的成員與 MCP `list_routes` 相同。`GET /api/routes/{name}` 的回應不變。
- `GET /api/dashboard` 的每個成員另帶 `daily_spend_7d`：最近 7 天實際花費 ÷ 7（USD，不四捨五入；無花費時為 0.0；沒有上限或沒有額度設定的成員也輸出）；`est_days_left`（5.4.6）由它推導。
- `min_depth`（`GET /api/ledger`、`GET /api/ledger/export.csv`）：整數 0–100，只列 `fallback_depth >= min_depth` 的列；超出範圍或不是整數回傳 HTTP 422，錯誤碼 `validation_error`。UI-6 的「只看 fallback」送 `min_depth=1`。
- `GET /api/mcp/catalog?group=<name>`：`group` 預設 `all`；回應為 `mcp_catalog(app, group)`：`{"group", "connection": {"transport", "url", "groups": [{"name", "url"}], "auth", "stdio"}, "tools", "notifications", "errors": {"http", "tool", "sdk_transport"}}`。群組不存在且不是 `all` 時回傳 HTTP 404，錯誤碼 `not_found`。內容不含 token，只以環境變數 `SPILLWAY_TOKEN` 參照；不列 `/api/*` 與 `/v1`。

### 9.3 拖曳儲存格式

```json
PUT /api/routes/review/order
{
  "slots": [
    [{"deployment": "claude-sub", "effort": "high", "weight": "high"}],
    [{"deployment": "anthropic-api", "effort": "medium", "weight": "medium"},
     {"deployment": "deepseek-api", "effort": "medium", "weight": "high"}],
    [{"deployment": "qwen-local"}]
  ]
}
```

9.3.1 Body 中的成員必須屬於該 route 目標；否則回傳 422，錯誤碼 `member_not_in_target`。

9.3.2 儲存必須以單一交易取代該 route 的全部 `route_targets`。

## 10 Web UI 畫面清單

Web UI 共 8 個畫面，左側固定導覽，網址為 `http://127.0.0.1:<port>/ui/`（`GET /` 轉址到 `/ui/`；首次設定精靈為 `/ui/wizard.html`）。畫面是靜態 HTML、CSS 與 JavaScript，不經建置、不用模板、不用第三方前端套件；所有資料由瀏覽器以同源請求呼叫 `/api/*` 取得（UI-8 經 `GET /api/mcp/catalog`），伺服端只提供這些檔案（只允許 GET、HEAD；只提供 `.html`、`.css`、`.js`，點檔與其他檔案回傳 404）。`/` 與 `/ui` 的回應帶 `Content-Security-Policy`（只允許同源的 script、style、圖片與連線，含 Trusted Types，禁止被嵌入框架）、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`Cross-Origin-Opener-Policy: same-origin`、`Cross-Origin-Resource-Policy: same-origin` 與 `Cache-Control: no-cache`；`/api` 的回應一律帶 `Cache-Control: no-store` 與 `X-Content-Type-Options: nosniff`。畫面不連外（不載入外部字型或任何外部資源，使用系統字型），使用者資料（成員設定、帳本內容等）一律以純文字顯示。本段依企劃書第六節「細節於規格書定案」決定（CHG-20261103-01）。UI-1–UI-7 的 layout 見 [UI layout 設計稿](ui/)（實作與設計稿的差異見 `sdlc_docs/structure/design.md`「設計差異」），UI-8 沒有 layout 稿；目的與範圍見 [企劃書](proposal.md)。

| 編號 | 畫面 | 用途 | 主要操作 |
| --- | --- | --- | --- |
| UI-1 | 總覽 | 各成員剩餘額度、預估可用天數、近 7 天日均花費、冷卻狀態、近 24 小時請求；摘要卡顯示冷卻中成員數與近 24 小時請求數。不提供期間切換（額度期間由各成員的 `window` 決定） | 清除冷卻、跳至成員 |
| UI-2 | 成員 | 成員清單與編輯（類型、供應商、模型以選單選取；能力、額度、tag、憑證參照） | 新增、測試呼叫、啟用切換 |
| UI-3 | 群組與 Tag | 以 all、any、none 編輯選擇器，即時預覽符合的成員（`POST /api/groups/preview`） | 新增群組、編輯 tag（列出全部 tag 與各 tag 的成員，點選成員到 UI-2 修改其 tags；tag 改名延後，CHG-20261103-01 決策 24） |
| UI-4 | Route | Route 清單；編輯頁以拖曳調整 slot 與成員順序，逐成員以選單設定 effort 與優先權重（高、中、低）；route 層級勾選「預估額度檢查」與「記錄 context」 | 拖曳、還原預設、dry-run |
| UI-5 | MCP 設定 | 選群組與工具，顯示各放置方式（`variants`）的設定內容、放置路徑與注意事項（8.2.1） | 複製、下載 |
| UI-6 | 帳本 | 每次嘗試的紀錄，含預估與實際 token、花費；點選列開啟請求與回應內容 | 篩選、匯出 CSV |
| UI-7 | 設定 | 首次設定精靈（獨立頁 `/ui/wizard.html`，UI-7 頁首有連結；`GET /api/settings` 的 `first_run` 為 true 時開啟總覽 `/ui/`（含 `/`）即導向精靈）、YAML 匯入匯出、存取 token、埠號（唯讀）、context 記錄預設與保存天數 | 匯入（選檔與 `mode` 後按「預覽匯入」才送 `dry_run=true`；確認對話框顯示新增、更新、刪除計數與 `privileged` 成員 id，按「匯入」才以同一份內容寫入；檔案或 `mode` 改變後先前的預覽作廢）、匯出、重新產生 token（新 token 只在對話框顯示一次，關閉時清除，不寫入瀏覽器儲存或網址） |
| UI-8 | API 參考 | 只列 MCP 介面，內容於執行期產生、與程式一致（資料來源 `mcp_catalog(app, group)`），依類型分四大類：連線（各群組端點 URL、傳輸、驗證方式、`mcp-stdio`）、工具（`ask`、`list_routes`、`quota_status` 的說明與 inputSchema／outputSchema）、通知（`tools/list_changed`）、錯誤碼；不列 `/api/*` 與 `/v1`。版面：頂端群組選單（預設 `all`）、左側可折疊索引（四大類，各類下列項目）、右側顯示所選項目的內容 | 以可折疊索引切換大類與項目、切換群組 |

### 10.1 拖曳規則（UI-4）

10.1.1 成員卡片可拖至既有 slot 或「新 slot」區；slot 本身可整列拖曳改變順序。

10.1.2 拖曳後 slot 為空時必須自動移除。

10.1.3 未儲存的變更必須標示於畫面頂端，離開頁面前提示。

10.1.4 每個 slot 必須顯示內部成員的類型標籤與剩餘額度，讓使用者在拖曳時看得到可用狀態。

### 10.2 選單規則（UI-2、UI-4）

10.2.1 選單內容必須取自 `GET /api/adapters`（9.2.2），不可寫死在畫面中。

10.2.2 UI-2 建立與編輯成員時，必須依序以連動選單選取類型（`kind`）、供應商（`provider`）、模型（`model`）：供應商選單只列支援所選類型的模組；模型選單只列該模組的可用模型清單，以 `label` 顯示、以 `id` 寫入。所選模組為通用後備 `litellm` 時另顯示「LiteLLM provider」欄位（選定模型時帶入該模型在 `models.yaml` 宣告的 provider；空白即 `litellm`）。編輯既有成員而其供應商不在選單時：沒有對應模組的 api、local 成員以 `litellm` 後備開啟，原 provider 顯示在「LiteLLM provider」欄位（7.6.9）；其他情況仍顯示原值並標示原因。UI-2 的成員清單置於編輯表單上方（CHG-20261103-01）。

10.2.3 選定模型後，系統必須以該模型的 `capabilities` 帶入能力欄位；使用者仍可修改。

10.2.4 不可用的模組（7.6.7）必須在供應商選單中停用並顯示原因；`single` 模組已有成員時亦須停用，並註明「已有 1 個，僅限 1 個」。

10.2.5 模型選單最後一項為「其他（手動輸入）」，選取後可輸入模型 `id`，能力欄位須手動填寫。

10.2.6 UI-4 的 effort 選單只可列出該成員 `capabilities.efforts` 所含等級與「不指定」；優先權重選單為高、中、低。例外：手動排序中已存的 effort 不在 `capabilities.efforts` 內時（例如模型能力後來改變），選單另列該值並標「不在能力清單」，以免儲存時被改掉（CHG-20261103-01 決策 14）。

## 11 非功能需求

| 編號 | 項目 | 要求 | 量測方式 |
| --- | --- | --- | --- |
| NFR-1 | 路由決策耗時 | P95 < 20 ms（不含上游呼叫） | 100 個成員、當月 10,000 列帳本，連續 1,000 次 dry-run |
| NFR-2 | Dashboard 載入 | `/api/dashboard` P95 < 200 ms | 同 NFR-1 資料量 |
| NFR-3 | 網路暴露 | 預設只綁 127.0.0.1 | 啟動後檢查監聽位址 |
| NFR-4 | 憑證 | 資料庫、匯出檔、日誌不含任何金鑰或 token 明文 | 以已知金鑰字串搜尋三者的內容，命中數必須為 0 |
| NFR-5 | 帳本內容 | `usage_ledger` 不存 prompt 與回應；內容只存於 `usage_payloads`，過期後 1 小時內刪除 | 檢查 `usage_ledger` 欄位；保存天數設為 1 並推進系統時間 2 天，確認過期列已刪除 |
| NFR-6 | 並行 | CLI 成員同時執行數上限預設 1，可由 `params.max_concurrency` 調整；超過時視為不可用、不排隊 | 同時送出 2 個 task 請求，第 2 個必須由下一個成員處理 |
| NFR-7 | 執行環境 | Python 3.11 以上（Python 版專屬；Rust 版的工具鏈版本由 Rust 骨架 CHG 指定）；macOS、Linux、Windows | 三平台各執行驗收測試一次（雜湊 lock 只在 macOS 驗證，CHG-20261107-01） |

## 12 驗收標準

每條驗收項對應企劃書第三節的目標編號；全部通過才算完成。

自動驗收以黑箱方式執行（CHG-20261107-03）：behave、acceptance 與畫面測試只經 HTTP（`/v1`、`/api`、`/mcp`）、子行程（`serve`、`init`、`mcp-stdio`，依第 14 節）與唯讀開啟的資料庫檔案操作 spillway；時間、亂數與資料種入經第 13 節的測試控制介面。驗證遮罩的驗收項（AC-15、NFR-4）只走真實請求，不使用種入。

| 編號 | 對應 | 前置條件 | 操作 | 預期結果 |
| --- | --- | --- | --- | --- |
| AC-1 | G1 | route `review` 為預設順序，claude-sub 有額度 | 送出 10 次 task 請求 | 10 次皆由 claude-sub 處理 |
| AC-2 | G1 | 同上，使用者將 deepseek-api 拖至 slot 1 並儲存 | 送出 10 次 task 請求 | 10 次皆由 deepseek-api 處理 |
| AC-3 | G4 | slot 1 成員皆設為冷卻中 | 送出 1 次請求 | 由 slot 2 成員完成，客戶端只送 1 次 |
| AC-4 | 4.4 | slot 內 A 為 high、B 為 low，兩者額度比例皆為 1 | 送出 400 次 dry-run | A 被選比例介於 70% 至 80%（理論值 75%） |
| AC-5 | 4.1.1 | 有 local 成員 qwen-local | 送出 `model=local/qwen-local` | 由 qwen-local 處理，ledger `slot` 為 NULL |
| AC-6 | G2 | 每個成員皆設有額度 | 開啟 UI-1 | 每個成員顯示剩餘額度或冷卻至 |
| AC-7 | G3 | NFR-1 資料量 | 執行 1,000 次 dry-run | P95 < 20 ms |
| AC-8 | G5 | 空資料庫 | 依首次設定精靈加入 1 個 API 成員並呼叫 | 計時 ≤ 10 分鐘 |
| AC-9 | G6 | 群組 `coding` 已建立 | 將 `GET /api/mcp/config`（UI-5 的資料來源）輸出套用到 Claude Code、Codex、Cursor、Gemini CLI | 四者皆能列出並呼叫 `ask` |
| AC-10 | G7 | 已有完整設定 | 匯出後於空資料庫匯入 | 成員、群組、route、拖曳順序一致；匯出檔無憑證 |
| AC-11 | 8.1.6 | 群組 `coding` 已建立，持有有效 token | `/mcp` 請求帶 `X-Spillway-Depth: 1`，或以 `SPILLWAY_DEPTH=1` 執行 `mcp-stdio` 呼叫 `ask` | 被拒，錯誤碼 `recursion_blocked`（HTTP 403；`mcp-stdio` 回 JSON-RPC 錯誤 `data.code`，結束碼 3） |
| AC-12 | 2.1.4 | 已有 claude-cli 成員 | 再新增一個 claude-cli 成員 | 建立失敗，HTTP 409，錯誤碼 `provider_single`，並顯示原因 |

### 12.1 Context 記錄與預估

| 編號 | 對應 | 前置條件 | 操作 | 預期結果 |
| --- | --- | --- | --- | --- |
| AC-13 | 2.4.1 | 空資料庫、未改設定 | 送出 1 次請求 | `usage_payloads` 有 1 列，含請求與回應 |
| AC-14 | 2.4.5 | 保存天數 7 | 推進系統時間 8 天後執行清理 | 該列已刪除；`/api/ledger/{id}/payload` 回傳 410 |
| AC-15 | 2.4.4 | prompt 含 `sk-test-0000` | 送出請求 | 儲存內容中該字串已遮罩，`redacted = 1` |
| AC-16 | 4.2.5 | route 勾選預估檢查；claude-sub 剩餘 $0.01；請求預估 P90 為 $0.05 | 送出請求 | claude-sub 記為 `skipped_precheck`，由下一個成員處理 |
| AC-17 | 4.2.6 | route 未勾選；claude-sub 接近用完 | 持續送出請求至上游回報用完，再推進至重置點 | 用完後跳過並冷卻；重置後自動恢復被選用 |
| AC-18 | 5.4.3 | 某 route 與成員已有 50 筆 `ok` 紀錄 | 執行 dry-run | 顯示的 P50、P90 與帳本計算值一致 |

### 12.2 Adapter 模組與選單

| 編號 | 對應 | 前置條件 | 操作 | 預期結果 |
| --- | --- | --- | --- | --- |
| AC-19 | 7.1.2、7.6 | 內建模組皆可用 | 新增一個符合 7.6.1 的測試模組資料夾（宣告新 `provider`），不修改任何既有檔案，重新啟動 | `GET /api/adapters` 列出該模組；以該 `provider` 建立的成員可被路由呼叫；合約測試對它通過 |
| AC-20 | 7.6.4、10.2 | 內建模組皆可用 | 於 UI-2 依序選類型 `cli`、供應商 `claude-cli`、任一模型 | 供應商選單只列 `cli` 模組；模型選單與 `claude_cli` 的 `models.yaml` 一致；能力欄位帶入該模型的值 |
| AC-21 | 7.6.7 | 有一個 `__init__.py` 無法 import 的模組 | 啟動系統並開啟 UI-2 | 系統正常啟動；該模組於供應商選單停用並顯示原因；其成員候選篩選原因為 `adapter_unavailable` |

### 12.3 MCP 介面目錄（UI-8）

| 編號 | 對應 | 前置條件 | 操作 | 預期結果 |
| --- | --- | --- | --- | --- |
| AC-22 | 10（UI-8） | M4 Web UI 已啟動 | 開啟 UI-8 | 顯示四大類（連線、工具、通知、錯誤碼）與可折疊索引；內容與 `mcp_catalog(app)` 一致；不含 `/api/*` 與 `/v1` |

## 13 測試控制介面（僅測試模式）

本節為跨實作契約（CHG-20261107-03 決策 5–7）：黑箱測試以它控制時間、亂數與資料種入，任何 spillway 實作都必須提供同一組介面。**僅供測試，正式使用不得設定本節的環境變數。**

### 13.1 開啟條件

13.1.1 只有 `spillway serve` 在環境變數 `SPILLWAY_TEST_CONTROL` 的值為字面 `1` 時開啟控制介面；其他值（含空字串、`true`、`0`）視為未開啟。設定檔、管理 API、`init`、`mcp-stdio` 都無法開啟。實作中只有 serve 的啟動程序讀取本節的環境變數，再以明確參數交給 app。

13.1.2 開啟時 `SPILLWAY_HOME` 必須明確設定（非空），且不得與預設資料目錄 `platformdirs.user_data_dir("spillway")` 是同一個目錄（包括大小寫不同、符號連結、同一位置的其他寫法（例如 macOS 的 `/System/Volumes/Data` 前綴；目錄尚不存在時以最近存在的上層目錄比對），以及以帳號的家目錄推得的預設位置——`$HOME` 被覆寫時仍以帳號家目錄比對；Linux 另含 `~/.local/share/spillway`）；不符時在 stderr 印錯誤、以結束碼 2 結束，不建立資料目錄、不開啟資料庫。

13.1.3 開啟時 serve 在 stderr 印一行警告（14.3.2），控制介面掛在 `/__test__/` 之下，不列入 OpenAPI（serve 有提供 `/openapi.json` 時其中不得含 `/__test__`）。控制介面照舊經 9.1.4 的 Host／Origin 檢查、只綁 127.0.0.1，回應帶 `Cache-Control: no-store` 與 `X-Content-Type-Options: nosniff`。

13.1.4 未開啟時不掛載：所有 `/__test__`、`/__test__/*`（任何方法、結尾斜線、以 `%5F` 編碼底線的變形）回傳 HTTP 404，body 與不存在的路徑相同（`{"error": {"code": "not_found", "message": "Not Found"}}`）。

13.1.5 serve 啟動的 CLI 子行程（7.3、7.4）一律移除 `SPILLWAY_TEST_CONTROL` 與 13.2 列舉的旋鈕，不論是否開啟。

### 13.2 啟動期旋鈕

只在 `SPILLWAY_TEST_CONTROL=1` 時讀取；值不合法時 stderr 印錯誤（只含變數名與原因，不含值）並以結束碼 2 結束，不開啟資料庫。未設定或為空字串時取預設行為。

| 環境變數 | 值 | 作用 |
| --- | --- | --- |
| `SPILLWAY_TEST_START_TIME` | `YYYY-MM-DDTHH:MM:SSZ`（與 13.3.2 同一文法） | 以可控時鐘從該時刻開始（13.4.1）；未設定時用系統時鐘 |
| `SPILLWAY_TEST_RNG_SEED` | 完全符合 `-?[0-9]+` 的十進位整數，範圍 −2^63 到 2^63−1 | 路由抽選（4.4）的亂數種子（13.4.2） |
| `SPILLWAY_TEST_ADAPTER_PATHS` | 以作業系統路徑分隔字元（POSIX 為 `:`）分隔的路徑 | 額外載入的 adapter 模組所在目錄（Python 版為內含 7.6.1 模組資料夾的目錄，屬 Python 版專屬，見 15.11；實作相依） |
| `SPILLWAY_TEST_RETENTION_POLL_SECONDS` | 完全符合 `[0-9]+(\.[0-9]+)?` 且大於 0（秒；不接受指數、底線、`inf`） | 保存排程的輪詢間隔 `poll`（預設 30） |
| `SPILLWAY_TEST_RETENTION_STEPPED` | `1` | 保存排程改為 STEPPED 模式（13.4.6） |
| `SPILLWAY_TEST_NO_TOKEN` | `1` | 不簽發存取 token：不印 `TOKEN_NOTICE`，`token_issued` 為 false，`/mcp/*` 回 401 |
| `SPILLWAY_TEST_LOG_LEVEL` | `DEBUG` 或 `INFO` | 把日誌以 `SPILLWAY-LOG` 行輸出到 stderr（14.3.5），等級即該值 |

13.2.1 未開啟時設定了上表的變數：serve 在 stderr 印一行 `spillway: 未設 SPILLWAY_TEST_CONTROL=1,已忽略測試旋鈕: ` 加上有設定的名稱（依上表順序，以 `, ` 相接），不印值，其餘行為與未設定相同。只比對上表列舉的名稱，`SPILLWAY_TEST_` 不是保留前綴。

13.2.2 `SPILLWAY_TEST_GRACEFUL_TIMEOUT`（停機等待上限，秒）與 `SPILLWAY_TEST_MCP_MAX_SESSIONS`（MCP session 上限）是有意的例外：不受 `SPILLWAY_TEST_CONTROL` 限制，行為不變。

### 13.3 通用規則

13.3.1 請求與回應皆為 JSON；錯誤格式依 9.1.2。body 不接受未知欄位；欄位型別不符、缺少必要欄位回傳 HTTP 422，錯誤碼 `validation_error`。整數欄位不接受字串、小數或布林。

13.3.2 時間一律為 `YYYY-MM-DDTHH:MM:SSZ`（UTC，到秒，2.1.2；數字只限 ASCII `0-9`，每欄位數固定）；其他格式（含全形數字、時區位移、小數秒、只有日期）與不存在的日期回傳 HTTP 422，錯誤碼 `validation_error`。

13.3.3 所有 `/__test__/*` 的 GET 不改變資料庫狀態。

13.3.4 body 中值為 `null` 的欄位等同未給。請求先檢查 body 格式（422），再檢查成員或列是否存在（404），最後檢查狀態（409）。不支援的方法回 405（9.1.2 格式）。帶結尾斜線的路徑不屬於契約，測試不得使用。整數欄位的範圍是 −2^63 到 2^63−1，超出範圍回 422；推進後超出時鐘可表示的範圍同樣回 422。數值欄位接受 JSON 數字，不接受字串與布林，且必須是有限值。

13.3.5 錯誤碼：

| 狀態碼 | 錯誤碼 | 情況 |
| --- | --- | --- |
| 404 | `not_found` | 成員不存在（cooldowns、raw）；成員不存在或不是 `cli`（cli-slots） |
| 409 | `clock_not_controllable` | 系統時鐘模式下要求設定或推進時鐘（`clock`、`retention/tick` 的 `advance_seconds` > 0） |
| 409 | `retention_not_stepped` | 非 STEPPED 模式呼叫 `retention/tick` |
| 409 | `retention_not_running` | 保存排程未執行，或 tick 等待期間排程停止 |
| 409 | `retention_tick_timeout` | tick 等排程回到閘前逾時 |
| 409 | `cli_slots_exceeded` | `acquire` 後進行中數量會超過上限 |
| 409 | `cli_slots_underflow` | `release` 數量多於進行中數量 |
| 422 | `validation_error` | 13.3.1、13.3.2；種入違反資料庫約束（例如 payload 對應的帳本列不存在） |

### 13.4 端點

| 方法 | 路徑 | 請求 | 回應（200） |
| --- | --- | --- | --- |
| GET | `/__test__/clock` | — | `{"controllable": bool, "now": 時間}` |
| POST | `/__test__/clock` | `{"set": 時間}` 或 `{"advance_seconds": 非負整數}`（剛好一個） | `{"now": 時間}` |
| GET | `/__test__/rng` | — | `{"seeded": bool, "seed": 整數或 null}` |
| POST | `/__test__/ledger` | `{"rows": [帳本列, …]}` | `{"ids": [整數, …]}` |
| POST | `/__test__/payloads` | `{"rows": [內容列, …]}` | `{"ids": [整數, …]}` |
| POST | `/__test__/cooldowns` | `{"deployment", "until": 時間, "reason"}` | 同請求三欄 |
| POST | `/__test__/raw/deployment-params` | `{"id", "params": 任意 JSON}` | `{"id"}` |
| POST | `/__test__/retention/run` | — | `{"deleted": 整數或 null}` |
| GET | `/__test__/retention` | — | `{"runs": 整數, "next_due": 時間或 null}` |
| POST | `/__test__/retention/tick` | `{"advance_seconds": 非負整數}` | `{"ran": bool, "deleted": 整數或 null, "runs": 整數, "now": 時間}` |
| GET | `/__test__/health-cache` | — | `{"entries": [{"deployment", "healthy": bool}, …]}` |
| GET | `/__test__/cli-slots` | — | `{"slots": [{"deployment", "in_flight", "limit"}, …]}` |
| POST | `/__test__/cli-slots/acquire` | `{"deployment", "n": 正整數}` | `{"deployment", "in_flight", "limit"}` |
| POST | `/__test__/cli-slots/release` | `{"deployment", "n": 正整數}` | `{"deployment", "in_flight", "limit"}` |
| GET | `/__test__/mcp/sessions/{id}` | — | `{"stream_open": bool}` |

13.4.1 時鐘：設定 `SPILLWAY_TEST_START_TIME` 時為可控時鐘，`controllable` 為 true；可控時鐘凍結，只有 `set`（設為該時刻，可往前或往後）與 `advance_seconds`（往後推進）會改變它，真實時間經過不會。未設定時為系統時鐘：`GET` 回 200（`controllable` 為 false、`now` 為目前 UTC 時間），`POST` 回 409 `clock_not_controllable`。body 同時含或都不含 `set`、`advance_seconds` 時回 422。

13.4.2 亂數：`seeded` 表示啟動時是否設定 `SPILLWAY_TEST_RNG_SEED`，`seed` 為該值（未設定為 null）。seed 為不透明值：同一實作以同一 seed 啟動時抽選序列可重現，不同實作之間不保證序列相同。需要「無 seed」的情境以不設定該旋鈕重新啟動 serve 取得；本介面不提供執行中重設亂數。

13.4.3 帳本與內容：
- 帳本列的欄位為 `id`（選填，正整數）、`ts`（必填）、`status`（必填，字串）與 2.3 `usage_ledger` 的其他欄位（皆選填；`fallback_depth` 預設 0，其餘預設 null）。內容列的欄位為 `ledger_id`（必填，正整數）、`ts`（必填）、`request_json`（必填，字串）、`response_text`（選填）、`bytes`（必填，非負整數）、`truncated`、`redacted`（選填布林，預設 false）。
- 兩者皆為 upsert：帳本列帶 `id` 且該列已存在、內容列的 `ledger_id` 已存在時，整列取代（未給的欄位回到預設值）；帳本列不帶 `id` 時新增。`ids` 依請求順序列出寫入列的 `id`／`ledger_id`。整批在一個交易內寫入，任一列失敗時全部不寫入。
- 種入不經遮罩（2.4.4），驗證遮罩的情境不得使用種入。

13.4.4 冷卻：覆寫（upsert）該成員的 `cooldowns` 列，不比較新舊期限（期限可往前）；`reason` 只接受 `quota_exhausted`、`rate_limited`；成員不存在回 404 `not_found`。

13.4.5 損壞狀態：只把該成員 `deployments.params` 欄改為 `params` 的 JSON 序列化結果，不經 9.2 的成員驗證，其他欄位不變；成員不存在回 404 `not_found`。回應只含 `{"id"}`，不回顯 `params`；實作不記錄此端點的 body。測試放入的金鑰樣值一律以 `sk-fake` 開頭。

13.4.6 保存清理（2.4.5）：
- 保存排程啟動時先清理一次，之後在 `now >= next_due − 2 × poll` 時清理（`now` 為 13.4.1 的時鐘，`next_due` 為上次清理時刻加 1 小時，`poll` 為輪詢間隔）；每次清理後 `next_due` 更新為該次的 `now` 加 1 小時，不論成敗。
- `retention/run`：立即清理一次（不看到期判斷），`deleted` 為刪除的內容列數，清理失敗時為 null。`GET /__test__/retention` 的 `runs` 為成功的清理次數（含啟動時那一次），`next_due` 尚未排定時為 null。
- STEPPED 模式（`SPILLWAY_TEST_RETENTION_STEPPED=1`）：排程每一輪的輪詢等待換成一道閘，只有 `retention/tick` 會放行，真實時間經過不會觸發清理。`retention/tick` 依序：① 推進時鐘 `advance_seconds` 秒（系統時鐘模式下大於 0 時回 409 `clock_not_controllable`，不放行）；② 放行排程一輪（該輪照一般輪詢做到期判斷，到期才清理）；③ 等排程回到閘前才回應。`ran` 表示該輪是否清理、`deleted` 為該輪刪除列數（未清理或清理失敗為 null）、`runs` 與 `now` 為回應當下的值。並行的 tick 依序執行。非 STEPPED 模式回 409 `retention_not_stepped`；等待期間排程停止回 409 `retention_not_running`；排程未在時限內（30 秒）回到閘前回 409 `retention_tick_timeout`。

13.4.7 健康快取：列出 local 健康結果快取（4.2）中尚未過期的項目，依 `deployment` 排序；`now − 寫入時刻 ≥ TTL` 的項目視為過期、不列出（`now` 為 13.4.1 的時鐘）。

13.4.8 CLI 名額（4.2.4、NFR-6）：`slots` 列出全部 `kind` 為 `cli` 的成員（依 `id` 排序），`in_flight` 為目前佔用的名額數，`limit` 為該成員的併發上限（`params.max_concurrency`，預設 1）。`acquire` 佔用 `n` 個名額，佔用後會超過 `limit` 時回 409 `cli_slots_exceeded` 且不佔用任何名額；`release` 釋放 `n` 個，多於進行中數量時回 409 `cli_slots_underflow` 且不釋放。以本端點佔用的名額與實際 CLI 呼叫共用，路由篩選視為進行中（`concurrency_full`）。

13.4.9 MCP session：`stream_open` 表示該 session 的 GET 通知串流（8.1）是否已在伺服器端就緒；session 不存在時為 false。

## 14 行程契約

本節為跨實作契約（CHG-20261107-03 AM-7）：黑箱 harness 依本節啟動、等待與停止 spillway。

### 14.1 結束碼

`spillway serve`：

| 結束 | 情況 |
| --- | --- |
| 2 | 用法錯誤：命令列參數不合法、`--host` 不是 127.0.0.1、13.1.2 的資料目錄條件不符、13.2 的旋鈕值不合法；不開啟資料庫 |
| 1 | 開啟資料庫前的前置檢查失敗：埠無法綁定、資料目錄鎖（`spillway.lock`）已被占用或無法建立；不開啟資料庫、不執行 migration |
| 3 | 啟動失敗：啟動程序失敗（例如 7.6.6 模組衝突、migration 失敗）或開始監聽時綁定失敗 |
| 130 | 收到 SIGINT（Ctrl-C）：正常關閉後結束，不印 traceback |
| 以 SIGTERM 結束 | 收到 SIGTERM：正常關閉後，實作必須把 SIGTERM 重新送給自己，以該訊號結束（shell 看到 143，`subprocess` 看到 −15）；不得以 0 結束 |

正常關閉：停止接受新請求，等待進行中的請求至多停機上限（預設 30 秒），停止保存排程，終止仍在執行的 CLI 子行程。停機期間再收到 SIGINT 時立即取消進行中的請求並結束。

`spillway init [--rotate]`：0 成功（含 token 已存在、未變更）；1 資料目錄鎖已被占用（serve 正在執行）、migration 或資料庫失敗；2 用法錯誤。token 明文只印到 stdout（單獨一行），其他訊息走 stderr。

`spillway mcp-stdio`：依 8.1.2。

### 14.2 存取 token 單行（`TOKEN_NOTICE`）

14.2.1 serve 啟動時資料庫沒有存取 token 雜湊（且未設 `SPILLWAY_TEST_NO_TOKEN=1`）時，產生 token、只存雜湊，並在 stderr 寫出**一行**：前綴 `spillway: 已產生存取 token(只顯示這一次,資料庫只存雜湊): `（UTF-8；括號、逗號與冒號為 ASCII 字元，前綴以一個 ASCII 空白結尾），緊接 43 個字元的 token（字元集 `A-Z a-z 0-9 - _`），以 LF（`0x0A`）結尾。前綴的位元組（十六進位）為：

```
73 70 69 6c 6c 77 61 79 3a 20 e5 b7 b2 e7 94 a2 e7 94 9f e5 ad 98 e5 8f 96 20 74 6f 6b 65 6e 28
e5 8f aa e9 a1 af e7 a4 ba e9 80 99 e4 b8 80 e6 ac a1 2c e8 b3 87 e6 96 99 e5 ba ab e5 8f aa e5
ad 98 e9 9b 9c e6 b9 8a 29 3a 20
```

14.2.2 這一行必須在開始監聽埠之前寫出並 flush；每個資料庫只印一次（之後的啟動不印）。token 不經日誌輸出。

### 14.3 stderr

14.3.1 stderr 只承諾以下四種行（逐行、UTF-8、LF 結尾）；其他內容（伺服器的存取紀錄、錯誤訊息的措辭）不屬於契約，測試不得比對。

14.3.2 14.2 的 token 行。

14.3.3 控制介面開啟時（13.1.3）的警告行，完整內容為 `spillway: 警告:測試控制介面已開啟(SPILLWAY_TEST_CONTROL=1),/__test__/* 可改寫狀態;僅供測試,正式使用請勿設定`（冒號、括號、逗號、分號為 ASCII 字元）；在開始監聽之前寫出。

14.3.4 13.2.1 的「已忽略測試旋鈕」行（前綴 `spillway: 未設 SPILLWAY_TEST_CONTROL=1,已忽略測試旋鈕: `，後接名稱）。

14.3.5 `SPILLWAY_TEST_LOG_LEVEL` 的日誌行：每筆紀錄的第一行為 `SPILLWAY-LOG <等級> <logger 名稱> <訊息>`（等級為 `DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL`；訊息已代入參數；以單一 ASCII 空白分隔），訊息、例外 traceback 與 stack 的其餘每一行各自以 `SPILLWAY-LOG-CONT ` 開頭續行。低於設定等級的紀錄不寫出。這些行不經遮罩；logger 名稱與訊息內容屬於實作相依（跨實作比對只檢查等級格式合法且不含秘密，見 15.2）。

### 14.4 就緒與綁定

serve 就緒的判定為 `GET /api/settings/context` 回傳 HTTP 200 且行程仍在執行；只在 127.0.0.1 監聽（NFR-3）。

## 15 跨實作一致性契約

適用於 Python 版與 Rust 版(`spillway-rs`)。本節規定兩個實作對同一輸入必須產生語意相同的輸出。

本節用語:parity 指差分比對,即以相同輸入分別操作兩個實作,依本節規則比對輸出;語料為 parity 使用的固定輸入集合;情境為語料中的一組請求序列;runner 為執行 parity 的測試程式;fakeapi 為 parity 使用的假上游 API,扮演 AI 供應商並記錄收到的請求。

### 15.1 裁決順序

15.1.1 本規格為準。本規格未規定的行為,以 Python 版為參考實作。本規格明定為實作相依或不屬於契約者,不適用本條。

15.1.2 發現差異時,先判斷是規格缺漏(補規格)或任一側缺陷(修該側)。不得以修改測試迎合任一側結案。

15.1.3 litellm 升版的 CHG 必須重跑 parity 並處理差異。

### 15.2 比對面狀態表

| 比對面 | 比對單位 | 方式 | 狀態 | 對應 parity 情境 |
|--------|----------|------|------|------------------|
| HTTP 狀態碼 | 單一回應 | 整數相等 | 已定義 | `status` |
| 契約 header | 單一回應 | 15.4 | 已定義 | `headers` |
| JSON body | 單一回應 | 15.5、15.9 | 已定義 | `json_body` |
| 錯誤物件 | 單一回應 | 15.7 | 已定義 | `errors` |
| DB 資料列 | 情境結束時整庫 | 15.8 | 已定義 | `db_rows` |
| API 上游請求 | 單一上游請求 | 15.3 | 已定義 | `upstream_chat` |
| CLI 呼叫 | — | — | 待定義(CLI adapter 移植) | — |
| MCP 訊息序列與 `mcp-stdio` | — | — | 待定義(MCP 移植) | — |
| CSV 與 YAML 匯出 | — | — | 待定義(設定與資料目錄移植、Web UI API 移植) | — |
| 花費與 token 估算 | — | — | 待定義(路由與額度移植) | — |

Streaming 依 1.1.3 不涵蓋;`stream: true` 的 400 回應(錯誤碼 `streaming_unsupported`)由「錯誤物件」比對。

log 不列入比對面。parity 以 `SPILLWAY_TEST_LOG_LEVEL=INFO` 啟動;輸出的 `SPILLWAY-LOG` 與 `SPILLWAY-LOG-CONT ` 行只檢查 14.3.5 的等級格式合法且不含 sentinel(15.10.3);logger 名稱與訊息內容依 14.3.5 為實作相依,兩側行數也不比較。

### 15.3 上游請求

15.3.1 比對單位為單一上游請求。同一情境內,兩側依 fakeapi 收到的順序逐筆配對,筆數必須相等。

15.3.2 比對 method、path、query 參數名稱集合、JSON body(15.5)。query 值不記錄、不比對。

15.3.3 header 只比名稱,不記錄值。名稱不分大小寫。

下表的 header 兩側都必須存在;缺少時判定不一致,兩側都缺亦同。

| 上游請求 | 必須存在 |
|----------|----------|
| `POST /v1/chat/completions`,成員有 `credential_ref` | `authorization`、`content-type` |
| `POST /v1/messages`,成員有 `credential_ref` | `x-api-key`、`content-type` |
| `POST`,成員沒有 `credential_ref` | `content-type` |
| `GET` | 無 |

Gemini 協定隨設定檔驅動 adapter 補入本表。

下表的 header 不比對。

| 條件 | 忽略 |
|------|------|
| 一律 | `user-agent`、`x-stainless-*`、`accept`、`accept-encoding`、`connection`、`content-length`、`host` |
| 成員沒有 `credential_ref` | `authorization`、`x-api-key` |

其餘名稱只出現在一側時判定不一致。

15.3.4 非 dict 的 JSON body 也納入比對;非 JSON body 只比長度。

### 15.4 契約 header

只比對下表的 header,其他 header 不比。名稱不分大小寫。兩側都缺時相等,一側缺判定不一致。

| header | 適用 | 值的比對 |
|--------|------|----------|
| `X-Spillway-Served-By`、`X-Spillway-Slot`(9.1.3) | `/v1/chat/completions` | 字串逐字(`local/` 直呼的 slot 為 `direct`) |
| `X-Spillway-Est-Cost`(5.4.5) | `/v1/chat/completions` | 「花費與 token 估算」待定義期間只比形狀 `^([0-9]+\.[0-9]{8})?$`(無預估時為空字串),值不比 |
| `cache-control`、`x-content-type-options`(13.1.3) | 全部路徑,9.1.4 的 403 與 5xx 回應除外 | 字串逐字 |

`Content-Type`、`Allow`、`Content-Disposition` 等於 Web UI API 移植時補入本表。

### 15.5 相等定義

15.5.1 bool、null、數值、字串、陣列、物件彼此不相等。缺鍵與 null 不相等。

15.5.2 物件不計鍵順序。陣列預設有序;無序陣列須於 15.9 逐欄列出。

15.5.3 數值以 JSON 數值比較(`1` 等於 `1.0`)。必須為整數的欄位逐欄列於 15.9,另比型別。

### 15.6 時間

15.6.1 parity 一律以 `SPILLWAY_TEST_CONTROL=1`、符合 13.1.2 的 `SPILLWAY_HOME` 與 `SPILLWAY_TEST_START_TIME` 啟動;時間欄位不設例外,依 15.5、15.8 逐字比對。

15.6.2 `latency_ms`(路徑列於 15.9.5):兩側皆 null 視為相等;一側 null 判定不一致;否則兩側都必須為整數且 `>= 0`,值不比。

### 15.7 錯誤與自由文字

15.7.1 `error.code` 與 HTTP 狀態碼永遠比對。

15.7.2 下列訊息逐字比對:`no_candidate`(形狀見 4.5.3)、13.1.4 不存在路徑的 404(`Not Found`)。

15.7.3 HTTP 422 只比對欄位路徑集合。`error.message` 依 `; ` 切分(9.1.2),每段第一個 `: ` 之前的文字完全符合 `^(body|query|path|header|cookie)(\.[^\s.:;]+)*$` 時為欄位路徑,原因不比;不符合的段落不比。語料的 422 案例每案只違反一個欄位。畸形 JSON 案例由語料標記,只比 15.7.1。

15.7.4 下列上游原文只比有無(皆為非空字串,或皆為 null)與不含 sentinel(15.10.3):`usage_ledger.error`、15.9.5 標 15.7.4 的路徑、15.7.2 未列的 5xx 訊息(含 `internal_error`)。

15.7.5 回應符合 9.1.2 形狀時,`error.message` 只依本節比對,不套用 15.5;`error` 下的其他鍵與 body 其他鍵依 15.5。本節未列的 `error.message` 只比型別為非空字串。

15.7.6 非 JSON 回應(9.1.4 伺服器層 400、CSV、YAML、HTML)在對應比對面定義前,只比 HTTP 狀態碼與 15.4 的 header。

15.7.7 本節只規範跨實作比對;共用測試既有的子字串斷言不受影響。

### 15.8 資料庫

15.8.1 情境最後一個回應收到後,以唯讀連線取快照。比對下列 10 張表的全部資料列,依主鍵排序:`deployments`、`deployment_tags`、`budgets`、`cooldowns`、`groups`、`routes`、`route_targets`、`usage_ledger`、`usage_payloads`、`settings`;不含 `sqlite_%`。`PRAGMA user_version` 必須相等。未列於 15.8.3 的欄位依 `typeof()` 相同且值相等比對。

15.8.2 JSON 文字欄位的規範形式為 `json.dumps(value, ensure_ascii=False, sort_keys=True)` 的輸出:分隔符 `, ` 與 `: `,鍵依 Unicode code point 遞增,非 ASCII 原樣輸出,浮點數形式同 Python `repr`。本規範形式只適用於 15.8.3 標 literal 的 JSON 欄位。`literal` 指遮罩與 15.9 替換後逐位元組相等。

15.8.3 下表各欄另比 `typeof()`,標明只比型別者除外。

| 欄位 | 方式 |
|------|------|
| `deployments.params` | 先依 15.9.5 替換 `$.api_base`,其餘 literal |
| `deployments.capabilities`、`groups.selector`、`routes.target`、`routes.tags` | literal |
| `usage_payloads.request_json` | `truncated = 0` 且兩側都能解析為 JSON 時,解析後依 15.5 比對;其餘情況(截斷、CLI prompt)逐字比對 |
| `usage_payloads.response_text` | 逐字(經遮罩)。tool_calls 形式隨內建協定 adapter 移植定義,在此之前語料不含 tool_calls |
| `usage_payloads.ledger_id`、`bytes`、`truncated`、`redacted` | 整數逐值 |
| `usage_payloads.ts` | 15.6.1 |
| `settings.access_token_sha256` | 比形狀 `^[0-9a-f]{64}$`,值不列入報告 |
| `settings` 其餘鍵 | literal |
| `usage_ledger.latency_ms` | 15.6.2 |
| `usage_ledger.error` | 15.7.4 |
| `usage_ledger.cost_usd`、`est_tokens_in`、`est_tokens_out_p50`、`est_tokens_out_p90`、`est_cost_usd_p90` | 「花費與 token 估算」待定義期間只比 `typeof()`,值不比 |

`usage_ledger.tokens_in`、`tokens_out` 來自上游 usage,逐值比對。

15.8.4 `usage_payloads` 的寫入形式(實作義務,與比對方式分開):`request_json` 為 `ensure_ascii=False`、分隔符 `, ` 與 `: `、不排序鍵的 JSON 文字,每個 message 先 `role`、`content`,其餘鍵依請求順序。寫入順序為序列化、遮罩(2.4.4)、截斷(2.4.3)。`bytes` 為寫入的 `request_json` 與 `response_text` 的 UTF-8 位元組長度之和。

### 15.9 正規化

15.9.1 只依欄位路徑逐項列出,不依值的樣式替換。未列路徑一律依 15.5 比對。

15.9.2 替換前驗證形狀;形狀不符判定不一致。標「遮罩前」的形狀在 15.10.1 ② 驗證。

15.9.3 以首次出現順序編號,例如 `<id-1>`。編號範圍為單一情境,依 15.2 表列的比對面順序,物件鍵排序後深度優先走訪;同值同號。

15.9.4 環境衍生值為 serve 綁定的埠、HOME(即本次 `SPILLWAY_HOME`)、fake `base_url`、`version`、`data_dir`。runner 已知實際值者,形狀驗證一律為「與本次實際值完全相等」。

15.9.5 下表為全部的正規化路徑與必須為整數的欄位。新增列必須說明該值為何屬於不定值或環境衍生值,並附突變案例。parity 程式內的路徑常數必須與本表一致,由自動測試比對本表與程式常數。

| 面 | 路徑 | 形狀或型別 | 處理 |
|----|------|------------|------|
| `POST /v1/chat/completions` | `$.id` | `^chatcmpl-[0-9a-f]{32}$` | `<id-n>` |
| 同上 | `$.created`、`$.choices[*].index`、`$.usage.prompt_tokens`、`$.usage.completion_tokens`、`$.usage.total_tokens` | 整數 | 不替換 |
| `GET /v1/models` | `$.data[*].created` | 整數 | 不替換 |
| `POST /api/settings/token/rotate` | `$.token` | 遮罩前 `^[A-Za-z0-9_-]{43}$`;遮罩後必須為 `[REDACTED]` | 不另替換 |
| `GET /api/settings` | `$.port` | 整數,等於本次埠 | `<port>` |
| 同上 | `$.data_dir` | 等於本次 `SPILLWAY_HOME` | `<home>` |
| 同上 | `$.version` | `^[0-9]+\.[0-9]+\.[0-9]+` | `<version>` |
| 同上 | `$.counts.*`、`$.context.context_retention_days`、`$.context.context_max_bytes`、`$.context.payload_count`、`$.context.payload_bytes` | 整數 | 不替換 |
| `GET /api/deployments` | `$.deployments[*].params.api_base` | 見 DB 列 | 同 DB 列 |
| `GET`、`PUT /api/deployments/{id}`、`POST /api/deployments` | `$.params.api_base` | 見 DB 列 | 同 DB 列 |
| `POST /api/deployments/{id}/test` | `$.latency_ms` | 15.6.2 | 不替換 |
| 同上 | `$.error` | 15.7.4 | 不替換 |
| 同上 | `$.cost_usd` | 待定義期間只比 null 或數值 | 不替換 |
| 同上 | `$.ledger_id`、`$.tokens_in`、`$.tokens_out` | 整數或 null | 不替換 |
| `GET /api/ledger` | `$.items[*].latency_ms` | 15.6.2 | 不替換 |
| 同上 | `$.items[*].error` | 15.7.4 | 不替換 |
| 同上 | `$.items[*].cost_usd`、`est_cost_usd_p90` | 待定義期間只比 null 或數值 | 不替換 |
| 同上 | `$.items[*].est_tokens_in`、`est_tokens_out_p50`、`est_tokens_out_p90` | 待定義期間只比 null 或整數 | 不替換 |
| 同上 | `$.items[*].id`、`tokens_in`、`tokens_out`、`slot`、`fallback_depth`、`$.page`、`$.page_size`、`$.total`、`$.pages` | 整數或 null | 不替換 |
| `GET /api/ledger/{id}/payload` | `$.request_json`、`$.response_text` | 同 15.8.3 同名欄位 | 不替換 |
| `GET /api/mcp/catalog` | `$.connection.url`、`$.connection.groups[*].url` | `^http://127\.0\.0\.1:<本次埠>/mcp/` | 只把埠換成 `<port>` |
| 同上 | `$.connection.stdio.args[*]` 中 `--port` 的下一項 | 等於本次埠 | `<port>` |
| 同上 | `$.connection.stdio.command` | `^spillway(-rs)?$` | `<cmd>`(15.11) |
| DB | `deployments.params` 的 `$.api_base` | 值以本次 fake `base_url` 開頭時替換;其他值不替換,依 literal 比對 | 把該字首換成 `<fake-base-url>` |

`GET /api/mcp/config` 依 `tool` 的路徑隨 MCP 移植補入本表。

### 15.10 秘密

15.10.1 每個擷取面依序處理:① 原始擷取只留在記憶體;② sentinel 掃描、15.10.5 的存在性記錄與 15.9.5 標「遮罩前」的形狀驗證;③ 金鑰遮罩(黑箱測試工具 `tests.blackbox.mask.mask_secrets`,`extra` 為語料金鑰值與本次 token 明文);④ 15.9 正規化;⑤ 比對。除 15.10.5 的布林值外,比較的是遮罩後的值。

15.10.2 語料金鑰只使用 `sk-test-`、`sk-fake` 開頭的假值。fakeapi 的 `echo_auth` 模式不得進語料。13.4.3 的種入內容不得含 sentinel。

15.10.3 sentinel 為語料金鑰值與本次 token 明文。掃描範圍:HTTP 回應的 header 與 body、DB 全部 TEXT 欄位、`SPILLWAY-LOG` 與 `SPILLWAY-LOG-CONT ` 行。命中判定不一致,報告只寫比對面與路徑,不寫值。下列位置不掃描:送往上游的請求 JSON body(使用者 prompt 依設計轉發)、`POST /api/settings/token/rotate` 的 `$.token`、13.4.5 故意種入的 `deployments.params` 及其 API 回顯。

15.10.4 差異報告、junit、`artifacts/` 與 runner 自己的記錄檔不得含金鑰或 token 值。

15.10.5 15.10.3 列為不掃描的位置中,送往上游的請求 JSON body 與 13.4.5 故意種入的 `deployments.params` 及其 API 回顯,在 15.10.1 ② 依遮罩前的內容,逐一記錄每個字串值是否含該側的 sentinel,只保留路徑與布林值,不保留值。資料庫欄位以整格為一個字串值。比對時,兩側同一路徑的布林值不同判定不一致,報告只寫比對面與路徑;路徑只出現在一側時依 15.5 判定,不另報。`POST /api/settings/token/rotate` 的 `$.token` 依 15.9.5,不適用本條。

### 15.11 允許差異與語料範圍

允許差異:

- Python 模組擴充(Python 版專屬,兩個實作共同的擴充形式由設定檔驅動 adapter CHG 定義;CHG-20261108-01 決策 3):7.6.1 的模組探索與 `SPILLWAY_TEST_ADAPTER_PATHS` 載入 Python 模組。
- 實作相依測試清單(`tests/blackbox/impl_dependent.toml`,列出換實作時須更換夾具的測試)中 `on_rust` 為 `replace` 或 `not_applicable` 的項目。
- MCP stdio 的 command(`GET /api/mcp/catalog` 的 `$.connection.stdio.command`)。

語料範圍:

- 每個 slot 至多一個雲端(`cli`、`api`)候選;跨 slot fallback 與 `local` 依 `position` 不受此限。只含循序請求。
- 不設 `budget_precheck`(4.2.5),不設 `usd` 額度上限。
- 「花費與 token 估算」轉為已定義前,不含 `POST /api/estimate`、`POST /api/routes/{name}/dry-run`、`GET /api/dashboard`。
- 設定檔驅動 adapter 定義前,不含 `GET /api/adapters`。
- 不可達的上游只用固定的 `http://127.0.0.1:9/v1`,不用隨機取得的空閒埠。

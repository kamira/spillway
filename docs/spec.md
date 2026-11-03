# Spillway — 規格書

2026-10-29 · Weiss Lin

修訂：2026-10-29 CHG-20261029-01（adapter 模組化、可用模型清單、後台選單、模組唯一或多個）

修訂：2026-10-30 CHG-20261030-02（7.1 Adapter 介面改為非同步；CallResult 加 tool_calls、finish_reason；5.3.1 加 anthropic 額度與過載訊號）

修訂：2026-11-01 CHG-20261101-03（schema v2：冷卻移至 `cooldowns` 表，`budgets` 移除 `exhausted_until`（2.2、2.3、4.5.2、5.3.2、5.3.3、9.2）；4.2.4 CLI 成員併發上限；6.3.2 模組 `default_params.effort_args` 視為已設定；7.3.2 子行程環境移除清單；7.4.3 固定以 `-c mcp_servers={}` 停用全部 MCP；9.2.4 成員與總覽回應的 `cooling_until`、`cooling_reason`）

修訂：2026-11-02 CHG-20261102-01（8.1 MCP server：群組端點與 session 綁定、群組名 `all` 保留、檢查順序、401 `unauthorized`、遞迴防護 403、工具輸出與錯誤結果、`list_changed` 送達範圍、`mcp-stdio` 結束碼；8.2 設定輸出一律以 `SPILLWAY_TOKEN` 參照 token、回應 `variants`／`notes`、模板與 `TOOL_FORMATS`、8.2.4 改為實作的實際輸出；新增 9.1.4 Host／Origin；9.2 `/api/mcp/config` 參數、`/mcp/<group>`、新增 9.2.5 群組名 `all` 保留；第 10 節 8 個畫面、UI-5 刪除「內嵌 token 切換」、新增 UI-8；AC-9、AC-11 改寫；新增 12.3 AC-22）

修訂：2026-11-02 CHG-20261102-02（3.1 設定檔規則：`merge`／`replace` 語意改為新增或整筆取代並刪除檔案外實體、`targets` 形狀、逐欄匯出規則、匯入請求邊界、嚴格解析、驗證與欄位路徑、套用順序、自動備份、設定檔為可信輸入與 `privileged`、`dry_run`、回應；3.2 範例 `limit` → `limit_amount` 並補欄位；8.1.2 `mcp-stdio` 依 body 且錯誤碼與狀態碼成對判斷拒絕、token 去除前後空白；8.1.3 token 去除前後空白；8.2.1 移除頂層 `format`、`path`、`content`；9.1.4 重複 `Host` 改寫為 400／403 兩路徑；9.2 新增 `/api/ledger/export.csv`、`GET /api/settings`，`/api/config/import` 註明 `mode` 必填與 `dry_run`；9.2.5 群組名稱規則；新增 9.2.6 帳本 CSV、9.2.7 設定總覽；第 10 節 UI-8 名稱「API 參考」）

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

3.1.12 設定檔是可信輸入：匯入等同以擁有者權限執行的設定（可改變憑證參照、上游位址與 CLI 參數），只可匯入可信來源。回應的 `privileged` 列出匯入後相對匯入前新增或變更了下列欄位的成員 `id`（依 `id` 排序；只含 id，不含值；被刪除的成員不列）：`credential_ref`；`params` 頂層的 URL 類鍵（`api_base`、`base_url`、`url`、`endpoint`，或以 `_url`、`_base`、`_endpoint` 結尾）；`kind: cli` 成員的 `params.extra_args`、`params.effort_args`、`params.cwd`。`params.timeout` 等其他鍵不列入。`dry_run=true` 時系統同樣解析、驗證、套用並檢查，之後回滾：不寫入、不寫備份、不讓健康快取失效、不發送 `list_changed`；回應同樣的計數與 `privileged`，`backup` 為 null；驗證失敗時回傳與非 `dry_run` 相同的錯誤。伺服端不強制確認 privileged 變更。

3.1.13 匯入成功回應：`{"mode", "dry_run", "created", "updated", "deleted", "backup", "privileged"}`；`created`、`updated`、`deleted` 各為 `{"deployments", "groups", "routes"}` 計數（`updated` 為檔案中且匯入前已存在的實體數，不論內容是否改變；`merge` 的 `deleted` 皆為 0）；`backup` 為 `backups/` 下的檔名，`merge` 與 `dry_run` 為 null。

3.1.14 已知限制：① 備份不自動清理，不驗證 `credential_ref` 指向的變數或 keyring 項目是否存在；② 3.1.10 ⑤ 的最終狀態檢查涵蓋整個資料庫，既有資料已違反規則（route 指向不存在的群組、`single` 群組有兩個同 provider 成員）時，每一次 `merge` 匯入都被擋下；③ 改變成員的 `provider` 不列入 `privileged`。

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

4.5.3 全部 slot 皆失敗時必須回傳 HTTP 503，錯誤碼 `no_candidate`，body 列出每次嘗試的成員與狀態。

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

5.2.1 `api`：必須使用 LiteLLM `completion_cost()` 的結果寫入 `cost_usd`；無定價的模型寫入 NULL 並於 UI 標示「未計價」。

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

5.4.2 輸入 token：以 `litellm.token_counter()` 依成員模型計算；CLI 成員另加 `params.cli_overhead_tokens`（初始 0，依帳本的預估與實際差異調整）。

5.4.3 輸出 token：請求帶 `max_tokens` 時，P50 與 P90 皆取 `max_tokens`；否則取同一 route 與成員最近 50 筆 `ok` 紀錄之 `tokens_out` 的 P50 與 P90；不足 5 筆時 P50 取 500、P90 取 2,000。

5.4.4 花費：以 LiteLLM 價格表計算；無定價的模型與 codex-cli 寫入 NULL，且不參與 4.2.5 的檢查。

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

LiteLLM 呼叫為上述三個模組共用的函式，不是獨立模組；7.2.1–7.2.3 適用於使用它的模組。

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

9.1.2 錯誤回應格式：`{"error": {"code": "<錯誤碼>", "message": "<說明>"}}`。

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
| GET | `/api/dashboard` | 各成員額度、本期花費、冷卻狀態、近 24 小時請求數 |
| GET / POST / PUT / DELETE | `/api/deployments[/{id}]` | 成員 CRUD（含 tag、額度） |
| POST | `/api/deployments/{id}/test` | 送出一次測試呼叫並回傳結果 |
| POST | `/api/deployments/{id}/clear-cooldown` | 清除冷卻（刪除該成員的 `cooldowns` 列） |
| GET / POST / PUT / DELETE | `/api/groups[/{name}]` | 群組 CRUD；GET 含符合的成員預覽；建立時的名稱規則（含保留字 `all`）見 9.2.5 |
| GET / POST / PUT / DELETE | `/api/routes[/{name}]` | Route CRUD |
| PUT | `/api/routes/{name}/order` | 儲存拖曳結果：body 為 slot 清單，每個 slot 含成員 id、effort、優先權重 |
| DELETE | `/api/routes/{name}/order` | 還原預設順序（4.3.4） |
| POST | `/api/routes/{name}/dry-run` | 以範例請求模擬路由，回傳候選與順序，不呼叫上游 |
| GET | `/api/ledger` | 帳本查詢；參數 `from`、`to`、`deployment`、`route`、`status`、`page` |
| GET | `/api/ledger/export.csv` | 帳本 CSV 匯出（9.2.6）；篩選參數同 `/api/ledger`，不分頁 |
| GET | `/api/mcp/config` | MCP 設定輸出（8.2）；參數 `group`（群組名或 `all`）、`tool`（`claude-code`、`codex`、`cursor`、`gemini`、`stdio`） |
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

9.2.6 帳本 CSV（`GET /api/ledger/export.csv`）：篩選參數與 `GET /api/ledger` 相同（`from`、`to`、`deployment`、`route`、`status`），不分頁，依 `ts`、`id` 由新到舊；符合的列超過 100,000 列時回傳 HTTP 422，錯誤碼 `validation_error`（請縮小範圍）。欄位與順序：`id, ts, route_name, deployment_id, effort, tokens_in, tokens_out, cost_usd, status, slot, fallback_depth, latency_ms, est_tokens_in, est_tokens_out_p50, est_tokens_out_p90, est_cost_usd_p90, error`；第一列為標題；`ts` 為 UTC ISO 8601；null 輸出空字串；不含 `usage_payloads` 內容。字串欄位以 `=`、`+`、`-`、`@`、Tab、CR 開頭時，系統必須在值前加 `'`（試算表公式注入防護），數字欄不加。回應 `Content-Type: text/csv; charset=utf-8`，內容以 UTF-8 BOM 開頭、行尾 CRLF，並帶 `Content-Disposition: attachment; filename="spillway-ledger-<UTC yyyymmddThhmmssZ>.csv"`、`Cache-Control: no-store`、`X-Content-Type-Options: nosniff`。上限以串流前的計數判斷；計數與串流讀取之間新增的列可能使輸出在第 100,000 列處截掉最舊的列。下載期間系統持有該次讀取的快照。

9.2.7 設定總覽（`GET /api/settings`）：回應 `{"port", "data_dir", "version", "token_issued", "first_run", "counts": {"deployments", "groups", "routes", "ledger_ok"}, "context": {"record_context_default", "context_retention_days", "context_max_bytes", "payload_count", "payload_bytes"}}`。`port` 為 serve 綁定的埠（唯讀、顯示用，不含綁定位址）；`data_dir` 為資料庫所在目錄（與 `backups/` 同一目錄）；`version` 為已安裝的套件版本；`token_issued` 表示存取 token 是否已產生（回應不含 token 或其雜湊）；`first_run` 為 `counts.deployments == 0` 且 `counts.routes == 0`（不看群組；以 `replace` 匯入空設定後再次為 true）；`ledger_ok` 為 `status = ok` 的帳本列數；`payload_count`、`payload_bytes` 為 `usage_payloads` 的列數與 `bytes` 加總。

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

Web UI 共 8 個畫面，左側固定導覽；UI-1–UI-7 的 layout 見 [UI layout 設計稿](ui/)，UI-8 沒有 layout 稿，版面於 M4b 的 CHG 決定；目的與範圍見 [企劃書](proposal.md)。

| 編號 | 畫面 | 用途 | 主要操作 |
| --- | --- | --- | --- |
| UI-1 | 總覽 | 各成員剩餘額度、預估可用天數、冷卻狀態、近 24 小時請求 | 清除冷卻、跳至成員 |
| UI-2 | 成員 | 成員清單與編輯（類型、供應商、模型以選單選取；能力、額度、tag、憑證參照） | 新增、測試呼叫、啟用切換 |
| UI-3 | 群組與 Tag | 以 all、any、none 編輯選擇器，即時預覽符合的成員 | 新增群組、編輯 tag |
| UI-4 | Route | Route 清單；編輯頁以拖曳調整 slot 與成員順序，逐成員以選單設定 effort 與優先權重（高、中、低）；route 層級勾選「預估額度檢查」與「記錄 context」 | 拖曳、還原預設、dry-run |
| UI-5 | MCP 設定 | 選群組與工具，顯示各放置方式（`variants`）的設定內容、放置路徑與注意事項（8.2.1） | 複製、下載 |
| UI-6 | 帳本 | 每次嘗試的紀錄，含預估與實際 token、花費；點選列開啟請求與回應內容 | 篩選、匯出 CSV |
| UI-7 | 設定 | 首次設定精靈、YAML 匯入匯出、存取 token、埠號、context 記錄預設與保存天數 | 匯入、匯出、重新產生 token |
| UI-8 | API 參考 | 只列 MCP 介面，內容於執行期產生、與程式一致（資料來源 `mcp_catalog(app, group)`），依類型分四大類：連線（各群組端點 URL、傳輸、驗證方式、`mcp-stdio`）、工具（`ask`、`list_routes`、`quota_status` 的說明與 inputSchema／outputSchema）、通知（`tools/list_changed`）、錯誤碼；不列 `/api/*` 與 `/v1` | 以可折疊索引切換大類 |

### 10.1 拖曳規則（UI-4）

10.1.1 成員卡片可拖至既有 slot 或「新 slot」區；slot 本身可整列拖曳改變順序。

10.1.2 拖曳後 slot 為空時必須自動移除。

10.1.3 未儲存的變更必須標示於畫面頂端，離開頁面前提示。

10.1.4 每個 slot 必須顯示內部成員的類型標籤與剩餘額度，讓使用者在拖曳時看得到可用狀態。

### 10.2 選單規則（UI-2、UI-4）

10.2.1 選單內容必須取自 `GET /api/adapters`（9.2.2），不可寫死在畫面中。

10.2.2 UI-2 建立與編輯成員時，必須依序以連動選單選取類型（`kind`）、供應商（`provider`）、模型（`model`）：供應商選單只列支援所選類型的模組；模型選單只列該模組的可用模型清單，以 `label` 顯示、以 `id` 寫入。

10.2.3 選定模型後，系統必須以該模型的 `capabilities` 帶入能力欄位；使用者仍可修改。

10.2.4 不可用的模組（7.6.7）必須在供應商選單中停用並顯示原因；`single` 模組已有成員時亦須停用，並註明「已有 1 個，僅限 1 個」。

10.2.5 模型選單最後一項為「其他（手動輸入）」，選取後可輸入模型 `id`，能力欄位須手動填寫。

10.2.6 UI-4 的 effort 選單只可列出該成員 `capabilities.efforts` 所含等級與「不指定」；優先權重選單為高、中、低。

## 11 非功能需求

| 編號 | 項目 | 要求 | 量測方式 |
| --- | --- | --- | --- |
| NFR-1 | 路由決策耗時 | P95 < 20 ms（不含上游呼叫） | 100 個成員、當月 10,000 列帳本，連續 1,000 次 dry-run |
| NFR-2 | Dashboard 載入 | `/api/dashboard` P95 < 200 ms | 同 NFR-1 資料量 |
| NFR-3 | 網路暴露 | 預設只綁 127.0.0.1 | 啟動後檢查監聽位址 |
| NFR-4 | 憑證 | 資料庫、匯出檔、日誌不含任何金鑰或 token 明文 | 以已知金鑰字串搜尋三者的內容，命中數必須為 0 |
| NFR-5 | 帳本內容 | `usage_ledger` 不存 prompt 與回應；內容只存於 `usage_payloads`，過期後 1 小時內刪除 | 檢查 `usage_ledger` 欄位；保存天數設為 1 並推進系統時間 2 天，確認過期列已刪除 |
| NFR-6 | 並行 | CLI 成員同時執行數上限預設 1，可由 `params.max_concurrency` 調整；超過時視為不可用、不排隊 | 同時送出 2 個 task 請求，第 2 個必須由下一個成員處理 |
| NFR-7 | 執行環境 | Python 3.11 以上；macOS、Linux、Windows | 三平台各執行驗收測試一次 |

## 12 驗收標準

每條驗收項對應企劃書第三節的目標編號；全部通過才算完成。

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

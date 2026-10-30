# 設計結構 — Spillway

- 專案:spillway · 分支:main · 版本:v1.2(2026-10-30,CHG-20261030-02:元件表與契約同步 M2 施工設計,SignalMatcher 移至 adapters.base,D9 依使用者決定 B1 修訂,新增 D16–D24;使用者於 CHG 確認閘確認,2026-10-30) · 前版 v1.1 已確認(2026-10-29,使用者;含 D11 adapter 模組化;v1.1 於 2026-10-30 確認 D8、D9、D13 手動輸入,CHG-20261030-01) · 日期:2026-10-30(UTC+0)
- 依據:sdlc_docs/ai-guideline.md v1.4;docs/spec.md;sdlc_docs/structure/logical.md

## 關鍵元件

| 元件 | 責任 | 對外介面 / 契約 | 規格 |
|------|------|-----------------|------|
| `Router` | 單次請求的完整路由:解析、篩選、排序、選擇、fallback | `Router(conn_factory, registry, clock, rng, *, close_conn=False, …)`;`async route(req) -> RouteOutcome`;`async dry_run(route_name_or_model, req) -> Plan`(不呼叫 adapter、不寫帳本);conn_factory 每請求呼叫一次、以 contextvar 共用,內建元件只在 route()/dry_run() 期間可用(CHG-20261030-02 施工審議 M19) | 4 |
| `Resolver` | `model` 字串 → `local/` 直呼 / route / 隱含 route / 404 | `resolve(model) -> Target` | 4.1 |
| `CandidateFilter` | 依 enabled、能力、mode、額度、冷卻、健康、預估篩選 | `async filter(target, req, *, probe=True, now=None) -> FilterResult(candidates, excluded)`;local 健康探測上限 2 秒、結果快取 10 秒(D20) | 4.2 |
| `OrderPlanner` | 手動 slot 或預設層級 → slot 清單;標示「未排序」 | `plan(route_targets, candidates, members=None) -> list[Slot]`;預設層級固定編號:cli 為 base、api 為 base+1、local 自 base+2 起(CHG-20261030-02 決策 38) | 4.3 |
| `SlotSelector` | slot 內加權隨機選雲端(權重 = 優先權重值 × `ratio()`)、失敗重選、再依 position 試 local | `SlotSelector(quota, rng).iterate(slot, now)`(generator,失敗後於剩餘成員重新加權) | 4.4 |
| `QuotaService` | 本期起點、下一重置點、剩餘額度與比例、冷卻判定、設定與清除;不含上游訊號判定(已移至 `adapters.base.classify`,D12 修訂) | `period_start(budget, now)`、`next_reset(budget, now)`、`remaining(dep, now)`、`ratio(dep, now)`、`is_cooling(dep, now)`、`mark(dep, status, now)`、`clear(dep)` | 5.1–5.3 |
| `classify`(原 `SignalMatcher`,位於 `adapters.base`) | 純函式:依成員 `params.signals` 或模組 `default_signals` 將上游輸出歸類;由 adapter 在 `call()` 內呼叫並回傳 status(CHG-20261030-02 決策 12) | `classify(raw, signals) -> ok / quota_exhausted / rate_limited / error` | 5.3.1、7.6.3、7.6.8 |
| `Estimator` | 輸入 token、輸出 P50/P90、P90 花費 | `estimate(dep, req, route_name) -> Estimate` | 5.4 |
| `Estimate` | 預估值物件 | `tokens_in`、`out_p50`、`out_p90`、`cost_usd_p90`(無定價時為 None) | 5.4 |
| `EffortMapper` | 來源優先序與降級(通用);上游參數值由各 adapter 模組的 manifest 提供,經 `adapters.base.effort_to_upstream(manifest, level)` 轉換(`none` 對應 None 時不傳) | `pick(route_target_effort, req_effort, caps_efforts) -> str \| None` | 6 |
| `Adapter`(Protocol) | 呼叫上游與健康檢查 | `async call(dep, req, effort) -> CallResult`;`async health(dep) -> bool`(CHG-20261030-02 決策 5,spec 7.1 同步改 async) | 7.1 |
| `AdapterContext` | `create(context)` 注入給模組的物件;新增欄位不算破壞契約、不升 `contract_version`(決策 13) | `credentials`(解析器)、`clock`、`cli_runner`(M2 為 None) | 7.6 |
| `AdapterManifest` | 每個 adapter 模組的宣告資料(見下方「Adapter 模組契約」) | dataclass,模組層級常數 `MANIFEST` | 6.3、5.3.1、7 |
| `AdapterRegistry` | 啟動時掃描 `spillway.adapters` 下的子套件並註冊;`provider` → adapter;未註冊依 `kind` 用通用 `litellm` 模組;提供 `cardinality_of(provider)` | `discover(extra_paths=()) -> list[AdapterManifest]`;`get(dep) -> Adapter`;`unavailable() -> dict[provider, reason]`;`cardinality_of(provider)`;`modules()` | 7.1.1 |
| `CliRunner`(M3) | 啟動 CLI 子行程:環境清理、深度標記、逾時、並行上限(非阻塞取得,滿了視為不可用) | `run(argv, stdin, env, cwd, timeout) -> ProcResult` | 7.3、7.4、NFR-6 |
| `Attempt` | Router 交給 LedgerWriter 的單次嘗試記錄:ts、route_name、deployment_id、effort、tokens、cost、status、slot、fallback_depth、latency_ms、est_*、error、請求與回應內容、`extra_secrets` | 型別,定義於 `ledger.py` | 2.4、4.5.1 |
| `Plan` | dry-run 結果:slots(每個候選含 deployment、kind、weight、effort、`Estimate`、健康狀態)、`excluded[]`(deployment、原因,含 `adapter_unavailable`、`skipped_precheck`)、`unsorted[]`、依 Rng 本次抽出的第一順位 | 型別,由 `Router.dry_run` 回傳 | 4 |
| `LedgerWriter` | 每次嘗試一列(呼叫後與結果同列一次寫入,D24);payload 先遮罩再截斷 | `record(attempt) -> ledger_id`;`payload_status(ledger_id, now) -> ok \| expired \| never_recorded` | 2.4、4.5.1 |
| `RetentionJob` | 每小時刪除過期 payload | `run_once(now)` | 2.4.5 |
| `Redactor` | 遮罩 `sk-…`、`Bearer` 後值、`extra_secrets` 中的每個值 | `redact(text, extra_secrets=()) -> (text, hit: bool)`;`matches(text) -> bool`(供寫入驗證) | 2.4.4 |
| `ConfigIO`(M4) | YAML ↔ 資料庫 | `export() -> str`;`import_(yaml, mode)`,失敗回欄位路徑 | 3 |
| `McpConfigRenderer`(M4) | 模板渲染各工具設定 | `render(group, tool, embed_token) -> {content, path, env}` | 8.2 |
| `McpApp`(M3) | 依 `<group>` 暴露 route;list_changed | MCP tools `ask`、`list_routes`、`quota_status` | 8.1 |
| `Clock` / `Rng` | 時間與亂數來源;實作:`SystemClock`、`FakeClock`(`advance()`)、`SystemRng(seed=None)`、`SeqRng` | `now() -> datetime(UTC)`;`random() -> float` | 測試注入 |
| `domain` 值物件 | `Deployment`、`Budget`、`ChatRequest`、`CallResult` 定義於基礎層 `spillway/domain.py`;`adapters.base` 轉匯出,adapter 模組照舊只 import base | dataclass(frozen) | 7.1;CHG-20261030-02 施工審議 M1 |
| `probe_http` | local 端點健康探測(D18):對 URL 發一次 GET,任何 HTTP 回應即健康 | `async probe_http(url, timeout=2.0) -> bool`(不外拋、不跟隨轉址、不讀 proxy 環境變數) | 4.2.4;CHG-20261030-02 施工審議 M8 |
| `model_string` / `load_litellm` | 統一的 LiteLLM 模型字串規則(provider 為 `litellm` 時原樣使用 model,其他一律 `<provider>/<model>`,spec 7.2.1);LiteLLM 單一載入點(本地價格表、關閉 verbose 與遙測、不下載 HF tokenizer) | `model_string(dep) -> str`;`load_litellm() -> module` | 7.2.1;CHG-20261030-02 施工審議 M11、M13、M14 |
| `CallResult` 工具呼叫欄位 | `tool_calls`(OpenAI 格式 dict 清單)、`finish_reason`,皆可選 | spec 7.1(CHG-20261030-02 決策 28) | 7.1 |
| litellm 模組 `list_models` | 查 local 端點可用模型;端點取參數 `api_base`,未給時取環境變數 `SPILLWAY_LOCAL_API_BASE`,再無則 Ollama 預設 `http://127.0.0.1:11434/v1`(M2 暫行設定,M4 UI-2 改用既有 local 成員的 api_base) | `list_models(context, api_base=None)` | 7.6.4;CHG-20261030-02 決策 25 |

## 元件關係圖

```mermaid
classDiagram
  class Router {
    +route(req) RouteOutcome
    +dry_run(route, req) Plan
  }
  class Adapter {
    <<Protocol>>
    +call(dep, req, effort) CallResult
    +health(dep) bool
  }
  class classify {
    <<函式,adapters.base>>
    +classify(raw, signals) status
  }
  Router --> Resolver
  Router --> CandidateFilter
  Router --> OrderPlanner
  Router --> SlotSelector
  Router --> EffortMapper
  Router --> AdapterRegistry
  Router --> LedgerWriter
  Router --> QuotaService : mark(status)
  CandidateFilter --> QuotaService
  CandidateFilter --> Estimator
  SlotSelector --> QuotaService : 剩餘比例
  SlotSelector --> Rng
  QuotaService --> Clock
  Router --> Estimator : 呼叫前預估
  Router --> Plan : dry_run
  LedgerWriter --> Attempt
  AdapterRegistry --> Adapter
  Adapter <|.. LiteLLMAdapter
  Adapter <|.. ClaudeCliAdapter
  Adapter <|.. CodexCliAdapter
  AdapterRegistry --> AdapterManifest : discover()
  ClaudeCliAdapter --> CliRunner : M3
  CodexCliAdapter --> CliRunner : M3
  ClaudeCliAdapter --> classify
  CodexCliAdapter --> classify
  LiteLLMAdapter --> classify
  AdapterRegistry --> AdapterContext : create(context)
  LedgerWriter --> Redactor
  RetentionJob --> Clock
  McpApp --> Router : M3
  McpApp --> QuotaService : M3
```

## Adapter 模組契約(D11)

每個供應商(「一種 AI」)是 `src/spillway/adapters/` 底下的一個子套件,自成一個模組:

```
adapters/<module>/
├── __init__.py      # 匯出 MANIFEST 與 create()
├── adapter.py       # 實作 Adapter Protocol
├── models.yaml      # 可用模型清單(D12),純資料,改清單不必改程式
└── README.md        # 這個 AI 的接法、所需 CLI/環境變數、已知限制
```

`__init__.py` 必須提供:

| 名稱 | 內容 |
|------|------|
| `MANIFEST: AdapterManifest` | `providers`(此模組處理的 `provider` 值,如 `["claude-cli"]`)、`kinds`(`cli`/`api`/`local`)、`effort_map`(五級 → 上游值或 `None`,取代 spec 6.3 的共用表)、`default_signals`(`{quota, rate_limit}` 正規式,spec 5.3.1 預設)、`default_capabilities`、`default_params`(如 `timeout`、`effort_args`)、`requires`(需要的執行檔或環境變數,用於健康檢查與 UI 提示)、`fallback_for_kinds`(只有 litellm 模組宣告 `["api","local"]`)、`access`(`subscription-cli` / `api` / `local`,見 D14)、`cardinality`(`single` = 此 provider 全系統只能有 1 個成員;`multiple` = 可建多個,見 D15)、`contract_version`(目前 `1`);另可選 `list_models(context)`,執行期向上游查詢可用模型(例:local 端點的 `/v1/models`) |
| `create(context) -> Adapter` | 建立實例;`context` 為 `AdapterContext`,欄位只有 `credentials` 解析器、`clock`、`cli_runner`(M2 為 None,M3 起提供 `CliRunner`)——模組拿不到 `store`、`routing`(決策見 CHG-20261030-02 決策 13) |

規則:

1. **新增一種 AI = 新增一個子套件資料夾**,不修改任何既有檔案(含 registry)。spec 7.1.2「只可新增 adapter 檔案與註冊一行」的上限因此收緊為零行。
2. 模組只可 import `spillway.adapters.base`(Protocol、`CallResult`、`AdapterManifest`、`AdapterContext`、`classify`、`effort_to_upstream`、`litellm_call`;M3 起另有 `CliRunner`)、標準庫與 `pyyaml`;不可直接 import `litellm`、`spillway.credentials`、`spillway.clock`;**不可 import `routing`、`store`、`api`、`mcp`**。由 `tests/contract/test_layering.py` 以 AST 檢查。
3. 每個模組必須通過共用合約測試(對所有被發現的模組參數化執行):`tests/contract/test_adapter_contract.py` 檢查 manifest 欄位齊全且合法、`effort_map` 五級都有定義、`models.yaml` 可解析;`tests/contract/test_call_status.py` 檢查 `call` 對假上游的成功/用完/429/錯誤四種輸出回傳正確的 `CallResult.status`(adapter 在 `call()` 內以 `classify` 歸類)。新模組另放自己的假上游於 `tests/fakes/<module>/`;三個內建 API 模組共用 `tests/fakes/api_upstream/`(D17)。
4. 註冊衝突(兩個模組宣告同一 `provider`)→ 啟動失敗並列出兩個模組,不猜哪個優先。
5. 模組載入失敗(import 錯誤、manifest 不合法、`contract_version` 不支援)→ 該模組不註冊,原因記入 `AdapterRegistry.unavailable()`,其 provider 依 D21 推得;使用該 `provider` 的成員在候選篩選時視為不可用(原因 `adapter_unavailable`),並在 UI-2 成員頁顯示。其他模組照常運作。
6. 成員的 `params` 覆寫 manifest 的 `default_params` / `default_signals`(spec 5.3.1「可於 UI 修改」不變)。
7. **可用模型清單(D12)**:每個模組以 `models.yaml` 宣告可用模型,每筆 `{id, label, provider?, capabilities: {ctx, tools, vision, efforts[]}, note?}`。合約測試驗證清單可解析、`efforts` 只含五級值。模組若提供 `list_models`,執行期結果與 `models.yaml` 合併(同 `id` 以 `models.yaml` 的能力為準),查詢失敗只回 `models.yaml` 並標示原因。
8. **訂閱制 CLI 與 API 是不同模組(D14)**:同一家 AI 的訂閱 CLI 與官方 API 各自一個模組,各有自己的 manifest、可用模型清單、effort 對應與上游訊號——例如 `claude_cli`(訂閱,經 `claude -p`)與 `anthropic_api`(API key)。兩者共用的 LiteLLM 呼叫程式放在 `adapters.base.litellm_call`,是共用函式,不是模組。
9. **唯一或多個(D15)**:`cardinality` 由模組宣告。`single` 的 provider 建立第二個成員時,`api.admin` 與 `configio` 匯入在同一交易內拒絕並回傳錯誤碼 `provider_single`;`multiple` 則不限。內建模組:訂閱 CLI 為 `single`(企劃書第七節「每家訂閱只允許一個 CLI 成員」),API 與 local 為 `multiple`。
10. 第一版內建模組:

    | 模組 | provider | access | cardinality |
    |------|----------|--------|-------------|
    | `claude_cli` | `claude-cli` | subscription-cli | single |
    | `codex_cli` | `codex-cli` | subscription-cli | single |
    | `anthropic_api` | `anthropic` | api | multiple |
    | `openai_api` | `openai` | api | multiple |
    | `litellm` | 其他所有 api / local provider(通用後備,如 `deepseek`、`ollama`) | api、local | multiple |

    `anthropic_api`、`openai_api` 兩個專屬 API 模組對應兩個訂閱 CLI,**使用者確認**(2026-10-29);其他供應商先由通用 `litellm` 模組處理,日後要專屬模型清單再各自加模組。
11. 外部套件以 entry point 外掛**不在範圍內**(使用者 2026-10-29 選定)。

## 後台選單(D13)

Web 後台建立與編輯成員時以選單取代自由輸入,資料來源是新增的唯讀端點 `GET /api/adapters`:

```json
{"modules": [{"module": "claude_cli", "providers": ["claude-cli"], "kinds": ["cli"],
  "access": ["subscription-cli"], "cardinality": "single", "existing_members": 0,
  "builtin": true, "fallback_for_kinds": [],
  "available": true, "unavailable_reason": null, "models_error": null,
  "models": [{"id": "claude-sonnet-5-5", "label": "Sonnet 5.5",
              "capabilities": {"ctx": 200000, "tools": true, "vision": false,
                               "efforts": ["low","medium","high","max"]}}]}]}
```

| 畫面欄位 | 選單內容 | 連動 |
|----------|----------|------|
| UI-2 類型(kind) | `cli`、`api`、`local` | 篩選下一個選單 |
| UI-2 供應商(provider) | 支援該 kind 的已發現模組的 `providers`;不可用的模組灰掉並顯示原因;`single` 且已有成員的 provider 灰掉並註明「已有 1 個,僅限 1 個」 | 決定模型選單 |
| UI-2 模型(model) | 該模組的可用模型清單(`label` 顯示、`id` 寫入) | 選定後帶入能力預設(ctx、tools、vision、efforts),仍可修改 |
| UI-4 每成員 effort | 只列該成員 `capabilities.efforts` 有的等級 + 「不指定」 | — |
| UI-4 優先權重 | 高、中、低 | — |

清單沒有的模型:模型選單最後一項「其他(手動輸入)」,允許輸入 `id`,能力需手填。**使用者確認**(2026-10-30):保留手動輸入,上游新出的模型不必等改清單就能用。

## 介面 / API 契約

- HTTP、MCP、YAML、DDL 的契約以 `docs/spec.md` 第 2、3、8、9 節為準,本文件不重抄。
- `CallResult` 依 spec 7.1。adapter **不寫帳本、不設冷卻**——它只回報結果;歸類由 adapter 在 `call()` 內呼叫 `adapters.base.classify(raw, signals)` 完成並回傳 status,狀態更新由 `Router` 經 `QuotaService` 做。這讓 adapter 可以獨立以假上游測試。
- `Router.route` 回傳 `RouteOutcome { result | no_candidate, attempts[] }`;`attempts` 用於 503 body、MCP `ask` 輸出。`Router.dry_run` 回傳 `Plan`(候選順序、`excluded[]`、每個候選的 `Estimate`),不回 `RouteOutcome`。
- 錯誤一律走 spec 9.1.2 格式;錯誤碼:

  | 錯誤碼 | HTTP | 來源 | 說明 |
  |--------|------|------|------|
  | `route_not_found` | 404 | api.openai | 找不到 route |
  | `no_candidate` | 503 | api.openai | 全部 slot 失敗,body 列每次嘗試「成員:狀態」 |
  | `streaming_unsupported` | 400 | api.openai | 請求帶 `stream: true`(CHG-20261030-02 決策 7,使用者決定 B4) |
  | `member_not_in_target` | 422 | api.admin | `PUT /api/routes/{name}/order` body 的成員不屬於該 route 目標(spec 9.3.1) |
  | `recursion_blocked` | spec 8.1.6 | mcp.server(M3) | 請求帶 `X-Spillway-Depth` ≥ 1 |
  | `payload_expired` | 410 | api.admin | payload 已過保存期限 |
  | `provider_single` | 409 | api.admin | `single` 的 provider 建立第二個成員,或 `PUT` 變更 provider 違反 cardinality(D15、D22) |
  | `validation_error` | 422 | api.admin、api.openai | 欄位格式錯誤;欄位路徑寫在 `message`(D22) |
  | `not_found` | 404 | api.admin | 資源不存在,或 payload 從未記錄(D22) |
  | `conflict` | 409 | api.admin | 例如刪除仍被 route 參照的群組(D22) |
  | `internal_error` | 500 | api(全站) | 非預期例外(例如帳本寫入失敗,決策 35);message 只含例外型別(CHG-20261030-02 決策 42) |
  | `method_not_allowed` | 405 | api(全站) | HTTP 方法不允許(決策 42) |
  | `http_error` | 其他 | api(全站) | 其餘 HTTPException 的後備(決策 42) |

## 設計決策與取捨

| # | 決策 | 選項 | 取捨理由 |
|---|------|------|----------|
| D1 | 上游呼叫用 async | async(`litellm.acompletion`、`asyncio.create_subprocess_exec`) vs sync + threadpool | CLI 呼叫可達 600 秒;async 不佔執行緒,並行上限用非阻塞 semaphore 直接表達「滿了視為不可用」(NFR-6)。 |
| D2 | SQLite 用標準庫 `sqlite3`,短交易、每請求一連線 | `sqlite3` vs `aiosqlite` vs ORM | 單人寫入量小、查詢皆 ms 級;WAL 允許讀寫並行。ORM 對 11 張以內的表收益低,且 DDL 已在規格書定案。 |
| D3 | Schema 版本以 `PRAGMA user_version` 管理,啟動時依序套用 migration | user_version vs Alembic | 不引入 ORM 就不需要 Alembic;單檔資料庫用 user_version 足夠。 |
| D4 | 時間與亂數一律注入(`Clock`、`Rng`) | 注入 vs `freezegun` 等 monkeypatch | AC-4、AC-14、AC-17、NFR-5 需要「推進系統時間」與可重現分布;注入比全域 patch 穩定,也能用在背景清理工作。 |
| D5 | 假上游(Guideline Q1):`tests/fakes/` 放假的 `claude`、`codex` 可執行腳本(M3),測試時放到 `PATH` 最前面;API 的假上游見 D17(位於 `tests/fakes/api_upstream/`) | PATH 注入 vs 在成員設定加「指令路徑」欄位 | PATH 注入不需要改 spec 7.3.1 的命令定義;假腳本依環境變數決定回傳成功、用完、429、錯誤或延遲。API 的 mock 回應 vs 本機假 HTTP 端點在 CHG-20261030-02 定案為後者(D17)。 |
| D6 | MCP 用官方 `mcp` Python SDK 的 low-level server,單一 ASGI app 掛在 `/mcp/{group}`,依 path 參數決定暴露的 route | 低階 server vs 每群組一個 FastMCP 實例 | 群組會在執行期新增刪除;每群組一個實例要動態掛載與拆除,低階 server 依請求查表較單純,也方便發 `list_changed`。 |
| D7 | Web UI:Jinja2 伺服端渲染 + 原生 JavaScript(HTML5 拖放、`fetch` 呼叫 `/api/*`),不引入前端建置與框架 | 原生 JS vs htmx vs SPA | 企劃書已定「不引入前端建置流程」;7 個畫面中只有 UI-4 拖曳需要較多互動,原生 DnD 足夠。 |
| D8 | 資料目錄:環境變數 `SPILLWAY_HOME`,未設時用 `platformdirs.user_data_dir("spillway")`;內含 `spillway.db`、`work/claude/`、`tmp/` | 固定 `~/.spillway` vs platformdirs | 規格書只說「閘道資料目錄」;platformdirs 在三平台各自落在慣例位置(NFR-7)。**使用者確認**(2026-10-30)。 |
| D9 | 預設埠號 8787、綁定 127.0.0.1;存取 token **M3 起**於 `spillway init` 或首次啟動產生,只印一次,資料庫存 SHA-256;M2 不產生也不驗證 token,管理 API 在 M2 無驗證(前提為只綁 127.0.0.1) | (a) 只綁本機、token 延 M3 / (b) M2 完整做 token / (c) 先產生 token 不驗證 | 依 spec 8.1.3、9.1.1 與範例;8787 取自規格書範例。**使用者確認**埠號(2026-10-30)。CHG-20261030-02 依**使用者決定 B1**(2026-10-30)修訂:原文為「token 於 init 或首次啟動產生」,改為 M3 起;spec 9.1.1 只在綁定其他位址時要求 token。 |
| D10 | 設定檔 YAML 解析用 `PyYAML` safe_load,欄位驗證用 Pydantic 模型 | Pydantic vs 手寫驗證 | FastAPI 已帶 Pydantic;驗證失敗可直接產生欄位路徑(spec 3.1.2)。 |
| D11 | Adapter 一供應商一個子套件,manifest 宣告 + 啟動時自動發現 + 共用合約測試(見上方「Adapter 模組契約」) | 子套件自動發現 vs 加 entry point 外部外掛 vs 規格書原做法(單檔 + 註冊一行) | 使用者要求不同 AI 能以模組化方式加入。自動發現讓新增 AI 不碰既有檔案;effort 對應與上游訊號預設跟著模組走,不再集中在共用表。entry point 會載入 repo 外的第三方程式,第一版不採用。**使用者決定**(2026-10-29)。 |
| D12 | 可用模型清單放在各模組的 `models.yaml`,可選擇再加執行期查詢(CHG-20261030-02 更正:上游訊號歸類的 `classify` 為 `adapters.base` 的純函式,非應用層 `SignalMatcher`,見決策 12) | 模組內靜態清單 vs 資料庫表 vs 只靠執行期查詢 | 跟模組放在一起才符合「一種 AI 一個模組」:新增 AI 時清單一起帶進來;雲端 API 多半沒有可靠的列模型端點,所以靜態清單為主、執行期查詢為輔(local 端點可查)。**使用者要求**(2026-10-29),檔案形式代使用者決定。 |
| D13 | 後台以連動選單選取 kind → provider → model,資料來自 `GET /api/adapters`;保留「其他(手動輸入)」 | 選單 vs 自由輸入 | 使用者要求。選單直接反映已發現的模組與清單,打錯 provider 或模型名稱的機會降到零。 |
| D14 | 訂閱制 CLI 與 API 拆成不同模組;manifest 宣告 `access` | 同家共用一模組 vs 拆開 | 使用者要求(2026-10-29)。兩者的呼叫方式、計費、可用模型、上游訊號都不同;拆開後各自的清單與預設互不牽動。 |
| D15 | 模組宣告 `cardinality: single \| multiple`,由應用層在建立成員與匯入時強制 | 模組宣告 + 應用層檢查 vs 資料庫部分唯一索引(spec 2.1.4) | 使用者要求模組可限定唯一或允許多個。spec 2.1.4 的索引寫死「kind=cli 即唯一」,無法由模組決定,改為應用層依 manifest 檢查(同一交易內,防止並行建立)。AC-12 仍成立,因為 `claude_cli` 宣告 single。違反時回 HTTP 409、錯誤碼 `provider_single`(spec 7.6.5、9.2.3,CHG-20261029-01 寫入規格書時補定狀態碼)。 |
| D16 | 綁定位址的唯一來源:`serve --host`(預設 127.0.0.1)與 `--port`(預設 8787)加 settings.py 常數;M2 不讀 settings 表的 `bind_host`、`port` 鍵;允許集合只有字面 `127.0.0.1`,`localhost`、`::1`、`::`、`0.0.0.0`、LAN IP 一律拒絕並以非 0 退出 | 單一來源 vs 旗標、常數、settings 表三處 | B1 結論(M2 只綁本機);單一來源避免三處不一致,只接受字面 `127.0.0.1` 讓判斷沒有解析歧義。**代使用者決定**(CHG-20261030-02 決策 21)。 |
| D17 | 假 API 上游用本機假 HTTP 端點(`tests/fakes/api_upstream/`),同時提供 OpenAI 相容 `/v1/chat/completions` 與 Anthropic `/v1/messages`,LiteLLM 以 `api_base` 指向它 | 本機假端點 vs LiteLLM `mock_response`(D5 原列的兩個選項) | `mock_response` 會跳過 LiteLLM 的 HTTP 錯誤對應,402、429、`insufficient_quota` 的分類(spec 5.3.1)就沒被真的走過;anthropic_api 走 Messages 協定,所以兩種協定都要有。**代使用者決定**(CHG-20261030-02 決策 9)。 |
| D18 | 測試與驗收的 local 成員以 provider `hosted_vllm`(LiteLLM 以 OpenAI 相容協定呼叫)指向假端點;local 健康檢查 = 對 `api_base` 發 `GET`,2 秒內收到任何 HTTP 回應即健康 | 指定 `ollama` vs OpenAI 相容 provider | `ollama` 走原生 API,假端點要再多一套協定;健康檢查不依 provider 路徑,任何 local 端點都適用。真實 ollama 由擁有者冒煙驗證。**代使用者決定**(CHG-20261030-02 決策 10)。 |
| D19 | 額度邊界:`usd`/`credit_pool` 成員的 `limit_amount` 為 NULL,或 api 成員沒有 budgets 列時,視為無上限(剩餘無限、比例 1、不因額度被篩);上限為 0 時剩餘 ≤ 0、比例 0、被 spec 4.2.4 篩掉;剩餘 < 0 時比例夾到 0 | (a) 無上限,只記 design / (b) 視為不可用 / (c) 同 (a) 並改 spec | spec 4.4.2 只定義 0–1 與 `none` 以 1 計。**使用者決定 B3**(2026-10-30,CHG-20261030-02 決策 6):選 (a),只記入 design,不改 spec。 |
| D20 | local 健康檢查結果快取 10 秒;冷快取時 `route()` 同步探測一次(2 秒上限),`dry_run()` 不探測、將該成員標「健康未知」並照樣列為候選 | 每次探測 vs 短暫快取 | NFR-1 路由決策 P95 < 20 ms,探測逾時上限 2 秒;每次探測會讓 local 成員多的 route 超過 NFR-1,NFR-1 量測前先暖快取。誤判健康 → 呼叫失敗走 fallback 並寫帳本;誤判不健康 → 最多 10 秒不被選。**代使用者決定**(CHG-20261030-02 決策 20)。 |
| D21 | 無法 import 的模組,其 provider 依序由 (1) 以 `ast` 讀 `__init__.py` 中 `MANIFEST` 的 `providers` 字面值、(2) 資料夾名 `_` 轉 `-` 推得;推得的 provider 列入 `unavailable()`,不落到 litellm 後備 | ast 讀取 vs 只用資料夾名 vs 另加靜態宣告檔 | spec 7.6.7 要求使用其 provider 的成員為 `adapter_unavailable`;資料夾名規則對內建模組不成立(`anthropic_api` 的 provider 是 `anthropic`),所以先讀字面值;不新增檔案,不違反 spec 7.6.1。**代使用者決定**(CHG-20261030-02 決策 14)。 |
| D22 | 管理 API 錯誤碼與寫入驗證:新增 `validation_error`(422)、`not_found`(404)、`conflict`(409),422 一律轉成 spec 9.1.2 格式、欄位路徑寫在 `message`;`GET /api/ledger/{id}/payload` 以 `usage_ledger.ts` + 保存天數判斷,已過期回 410 `payload_expired`,從未記錄回 404 `not_found`;寫入時 `credential_ref` 必須為 `env:…` 或 `keyring:…:…`(否則 422 且不回顯值)、`params` 任何值命中 Redactor 樣式回 422 且不回顯、刪除仍被 route 參照的群組回 409 `conflict`、`PUT` 變更 provider 時於同一交易依 cardinality 檢查(違反回 409 `provider_single`)、`reset_anchor` 須為 1–28 | 新增錯誤碼 vs 沿用 FastAPI 預設 `{"detail": [...]}` | FastAPI 預設格式不符 spec 9.1.2;刪除 payload 後無法以 payload 列是否存在區分「過期」與「從未記錄」,故改以 ts 判斷;擋住金鑰本體被存進資料庫;spec 2.1.4 只寫「建立與匯入」,PUT 變更 provider 也檢查是補漏。**代使用者決定**(CHG-20261030-02 決策 16、17、22)。 |
| D23 | `local/` 直呼:`X-Spillway-Slot` 回 `direct`,帳本 `route_name` 為 NULL;隱含 route(spec 4.1.3):帳本 `route_name` 寫入該 `model_name` | — | spec 9.1.3 要求一律帶 Slot header;spec 5.4.3 依「同一 route」統計,隱含 route 需要穩定的統計鍵。**代使用者決定**(CHG-20261030-02 決策 18)。 |
| D24 | 帳本寫入:呼叫前計算預估,呼叫後與結果同列一次寫入 | 先插入再更新 vs 一次寫入 | `usage_ledger.status` 為 NOT NULL,五個值沒有 pending;崩潰時不留下不完整的列。對 spec 5.4.1「呼叫前計算並寫入」的解讀。並行請求可能同時看到同一份剩餘額度而超支,記為已知限制(單人使用)。**代使用者決定**(CHG-20261030-02 決策 19)。 |

## 採用模式

- **Strategy + 自動發現的 Registry**:`Adapter` 介面 + 每模組一份 manifest;新增供應商只加一個子套件資料夾(D11)。
- **Pipeline**:`Router` 內部是 Resolver → Filter → Planner → Selector 的固定管線,每段可單獨測試,也直接對應 dry-run 要回傳的中間結果。
- **設定驅動**:CLI 參數(`effort_args`、`extra_args`)、上游訊號規則(`signals`)、MCP 工具格式(模板檔)都是資料,不寫死(企劃書第九節風險對策)。
- **Ports & adapters 的測試縫**:`Clock`、`Rng`、`tests/fakes/api_upstream/` 的假 API 端點(M2);`CliRunner`、PATH 上的假 CLI(M3),讓自動測試不碰真實上游(Guideline Q1)。

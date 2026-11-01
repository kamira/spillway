# 設計結構 — Spillway

- 專案:spillway · 分支:main · 版本:v1.4(2026-11-01,CHG-20261101-03:M3a——`CliRunner` 介面改為決策 8、`AdapterContext.data_dir`、`claude_cli`/`codex_cli`、`adapters.base.cli_prompt`、Router 的 `cli_attempt` 守門與停機旗標、冷卻改存 `cooldowns`、新增 D28–D31,補錄 CHG-20261101-02 決策 14 為 D32;使用者於 CHG 確認閘確認,2026-11-01) · 前版 v1.3 已確認(2026-11-01,CHG-20261101-02:M2 整理——`REDACTOR` 共用實例、`normalize_effort` 移至 effort.py、`LiteLLMApiAdapter` 共用基底、健康快取失效、新增 D25–D27;使用者於 CHG 確認閘確認,2026-11-01) · 前前版 v1.2 已確認(2026-10-30,CHG-20261030-02:元件表與契約同步 M2 施工設計,SignalMatcher 移至 adapters.base,D9 依使用者決定 B1 修訂,新增 D16–D24) · v1.1 已確認(2026-10-29,使用者;含 D11 adapter 模組化) · 日期:2026-11-01(UTC+0)
- 依據:sdlc_docs/ai-guideline.md v1.5;docs/spec.md;sdlc_docs/structure/logical.md

## 關鍵元件

| 元件 | 責任 | 對外介面 / 契約 | 規格 |
|------|------|-----------------|------|
| `Router` | 單次請求的完整路由:解析、篩選、排序、選擇、fallback;CLI 嘗試的名額、子行程 env 憑證移除與遮罩、停機時不開始新嘗試(CHG-20261101-03 決策 7、9、16,D29、D31) | `Router(conn_factory, registry, clock, rng, *, close_conn=False, cli_runner=None, shutdown_flag=None, …)`;`shutting_down`(唯讀,讀 `shutdown_flag()`,app 傳入 `lambda: app.state.shutting_down`;讀取失敗視為 False);`cli_attempt(dep, conn=None, *, fallback=()) -> CliAttempt`(context manager,CLI 嘗試的共用守門,路由與管理 API 的 test-call 都經它,見 D29);`async route(req) -> RouteOutcome`;`async dry_run(route_name_or_model, req) -> Plan`(不呼叫 adapter、不寫帳本);conn_factory 每請求呼叫一次、以 contextvar 共用,內建元件只在 route()/dry_run() 期間可用(CHG-20261030-02 施工審議 M19);`invalidate_health(dep_id)` 移除該成員的健康快取結果(CHG-20261101-02 決策 12);回傳型別定義於 `routing.outcome`,`spillway.routing` 與 `spillway.routing.router` 轉匯出(決策 10) | 4 |
| `Resolver` | `model` 字串 → `local/` 直呼 / route / 隱含 route / 404 | `resolve(model) -> Target` | 4.1 |
| `CandidateFilter` | 依 enabled、能力、mode、額度、冷卻、CLI 併發名額、健康、預估篩選 | `CandidateFilter(registry, quota, estimator, clock, health_cache=None, *, cli_runner=None)`;`async filter(target, req, *, probe=True, now=None) -> FilterResult(candidates, excluded)`;local 健康探測上限 2 秒、結果快取 10 秒(D20);cli 成員 `cli_runner.in_flight(dep.id) >= max_concurrency(dep)` 時排除為 `concurrency_full`(排在 `cooling` 之後;`max_concurrency` 取 `params.max_concurrency` 正整數,否則 1)。`EXCLUDE_REASONS` 另含 Router 才會產生的 `shutting_down` | 4.2;NFR-6 |
| `HealthCache` | local 健康結果快取(D20),與 `Router` 共用同一實例;只有經 route 的請求讀寫,`local/` 直呼不讀 | `get(dep_id)`、`put(dep_id, healthy)`、`invalidate(dep_id)`(CHG-20261101-02 決策 12:管理 API 對成員 PUT〔含 `enabled` 變更〕、DELETE 成功後經 `Router.invalidate_health` 呼叫)、`clear()` | 4.2.4 |
| `OrderPlanner` | 手動 slot 或預設層級 → slot 清單;標示「未排序」 | `plan(route_targets, candidates, members=None) -> list[Slot]`;預設層級固定編號:cli 為 base、api 為 base+1、local 自 base+2 起(CHG-20261030-02 決策 38) | 4.3 |
| `SlotSelector` | slot 內加權隨機選雲端(權重 = 優先權重值 × `ratio()`)、失敗重選、再依 position 試 local | `SlotSelector(quota, rng).iterate(slot, now)`(generator,失敗後於剩餘成員重新加權) | 4.4 |
| `QuotaService` | 本期起點、下一重置點、剩餘額度與比例、冷卻判定、設定與清除;不含上游訊號判定(已移至 `adapters.base.classify`,D12 修訂)。額度讀 `budgets`,冷卻讀寫 `cooldowns`(schema v2,D28):`mark` 單一 upsert、冷卻只延長不縮短、不建立或修改 `budgets` 列;`clear` 刪除 `cooldowns` 列 | `period_start(budget, now)`、`next_reset(budget, now)`、`remaining(dep, now)`、`ratio(dep, now)`、`is_cooling(dep, now)`、`mark(dep, status, now)`、`clear(dep)` | 5.1–5.3 |
| `classify`(原 `SignalMatcher`,位於 `adapters.base`) | 純函式:依成員 `params.signals` 或模組 `default_signals` 將上游輸出歸類;由 adapter 在 `call()` 內呼叫並回傳 status(CHG-20261030-02 決策 12) | `classify(raw, signals) -> ok / quota_exhausted / rate_limited / error` | 5.3.1、7.6.3、7.6.8 |
| `Estimator` | 輸入 token、輸出 P50/P90、P90 花費 | `estimate(dep, req, route_name) -> Estimate` | 5.4 |
| `Estimate` | 預估值物件 | `tokens_in`、`out_p50`、`out_p90`、`cost_usd_p90`(無定價時為 None) | 5.4 |
| `EffortMapper`(`spillway/effort.py`) | 來源優先序與降級(通用);同模組另含 `normalize_effort(value) -> str \| None` 與 `InvalidEffort`(CHG-20261101-02 決策 8,自 router 移入;`spillway.routing`、`spillway.routing.router` 轉匯出);上游參數值由各 adapter 模組的 manifest 提供,經 `adapters.base.effort_to_upstream(manifest, level)` 轉換(`none` 對應 None 時不傳) | `pick(route_target_effort, req_effort, caps_efforts) -> str \| None` | 6 |
| `Adapter`(Protocol) | 呼叫上游與健康檢查 | `async call(dep, req, effort) -> CallResult`;`async health(dep) -> bool`(CHG-20261030-02 決策 5,spec 7.1 同步改 async) | 7.1 |
| `AdapterContext` | `create(context)` 注入給模組的物件;新增欄位不算破壞契約、不升 `contract_version`(決策 13) | `credentials`(解析器)、`clock`、`cli_runner`(預設 None;M3a 起 app 注入 `app.state.cli_runner` 同一個 `CliRunner` 實例)、`data_dir`(預設 None;app 注入資料庫所在目錄,CLI 模組以它推得 `work/<模組>`;CHG-20261101-03 施工審議補記) | 7.6 |
| `AdapterManifest` | 每個 adapter 模組的宣告資料(見下方「Adapter 模組契約」) | dataclass,模組層級常數 `MANIFEST` | 6.3、5.3.1、7 |
| `AdapterRegistry` | 啟動時掃描 `spillway.adapters` 下的子套件並註冊;`provider` → adapter;未註冊依 `kind` 用通用 `litellm` 模組;提供 `cardinality_of(provider, kind=None)` | `discover(extra_paths=()) -> list[AdapterManifest]`;`get(dep) -> Adapter`;`module_for(dep) -> ModuleInfo`(不可用時丟 `AdapterUnavailableError`,供 CandidateFilter 判定 `adapter_unavailable`);`unavailable() -> dict[provider, reason]`;`cardinality_of(provider, kind=None)`;`modules()` | 7.1.1 |
| `CliRunner`(`adapters.base.cli_runner`,M3a) | 共用子行程執行器,一個 app 一個實例(`app.state.cli_runner`):argv 陣列、不經 shell、`start_new_session=True`(子行程自成 process group);環境以目前環境為底移除 `env_remove` 與 `extra_env_remove`、再套 `env_set`;`stdin` 未給時為 DEVNULL;逾時、取消、停機一律 `os.killpg` 整個 group(SIGTERM,5 秒後 SIGKILL;重複遞送的取消改為立即 SIGKILL),主行程結束後最多 2 秒讀完管線;stdout/stderr 各自超過上限截斷並標記;不記錄 argv、stdin、環境與輸出。併發名額由 Router 取得,模組不碰(D29) | `async run(argv, *, stdin=None, env_remove=(), env_set=None, cwd, timeout, max_output_bytes=8 MiB) -> CliOutput(exit_code, stdout, stderr, timed_out, killed_reason, stdout_truncated, stderr_truncated)`(`killed_reason` ∈ `timeout`、`cancelled`、`shutdown`;取消時 `CancelledError` 照常往上丟;啟動錯誤如找不到執行檔、E2BIG 以 `OSError` 往上丟);`extra_env_remove: set[str]`(Router 每次 CLI 嘗試前設定,`run` 一律併入);`try_acquire(key, limit) -> bool`(同步、不排隊)、`release(key)`、`in_flight(key)`;`async terminate_all(grace)`、`kill_all_sync(grace)`(同步,不需事件迴圈;兩者設下不撤銷的停機旗標,之後才從 spawn 回來的子行程立即 SIGKILL、`killed_reason="shutdown"`);`active_pids`(唯讀,測試與診斷用)。CHG-20261101-03 決策 8 修訂原 `run(argv, stdin, env, cwd, timeout) -> ProcResult` | 7.3、7.4、7.6.3、NFR-6 |
| `messages_to_prompt`(`adapters.base.cli_prompt`,M3a) | spec 7.5.1 的唯一實作,兩個 CLI 模組共用(CHG-20261101-03 決策 12a):`system`/`developer`→`[System]`、`user`→`[User]`、`assistant`→`[Assistant]`(角色去空白、不分大小寫,保持順序);角色缺漏或空白視為 user;其他角色(tool、function、未知)整則略過;content 為字串照用,為清單時取字串項與 type 為 `text`、`input_text` 或未標 type 的文字項,其他部件略過,以換行連接;去空白後為空的訊息略過;各段 `"{標頭}\n{文字}"` 以空行連接。結果為空時模組回 `error` 且不啟動子行程 | `messages_to_prompt(messages) -> str`;`content_text(content) -> str`;`ROLE_HEADERS` | 7.5 |
| `ClaudeCliAdapter`(`adapters.claude_cli`,M3a) | `claude -p` 子行程:argv `claude -p --output-format json --model <model>` + effort 參數(`default_params.effort_args` `["--effort", "{effort}"]`,`none` 不傳)+ **固定** `--strict-mcp-config` + `params.extra_args`;prompt 經 stdin;移除 `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL`、`CLAUDE_CODE_USE_BEDROCK`、`CLAUDE_CODE_USE_VERTEX`、`CLAUDE_CODE_USE_FOUNDRY`,設 `SPILLWAY_DEPTH=1`;cwd 為 `params.cwd` 或 `data_dir/work/claude`;`tokens_in` = `input_tokens` + `cache_creation_input_tokens` + `cache_read_input_tokens`(各項只計非負整數,三者皆缺為 None),`tokens_out` = `output_tokens`,`cost_usd` = 有限非負的 `total_cost_usd`;`is_error` 或結束碼 ≠ 0 時以 `classify` 歸類(訊息只在 `is_error` 時取 `result`,否則取 stderr);逾時、停機終止、非 JSON、截斷、`OSError` 為 `error`。`health` 不起子行程、回 True;沒有 `list_models` | `MANIFEST`(provider `claude-cli`、single、`default_params` `timeout` 600、`effort_args`、`extra_args` `[]`)、`create(context)`(容忍 `cli_runner=None`,此時 `call` 回 `error`) | 7.3、5.2.2、5.3.1 |
| `CodexCliAdapter`(`adapters.codex_cli`,M3a) | `codex exec` 子行程:argv `codex exec --json --skip-git-repo-check -m <model> -o <暫存檔>` + `-c model_reasoning_effort=<值>` + `params.extra_args` + **固定** `-c mcp_servers={}`(`--` 前最後一個 `-c`)+ `--` + prompt;`extra_args` 含 `--` 或裸位置引數時回 `error`;`-o` 暫存檔 `tempfile.mkstemp`(0600)、`finally` 刪除;移除 `OPENAI_API_KEY`、`CODEX_API_KEY`、`OPENAI_BASE_URL`,設 `SPILLWAY_DEPTH=1`;cwd 為 `params.cwd` 或 `data_dir/work/codex`;回應取 `-o` 檔、token 取 JSONL 最後一個含 usage 的事件,`cost_usd` 為 None;結束碼 ≠ 0 時 stderr 與 stdout JSONL 的錯誤訊息一併交 `classify`;`OSError`(含 E2BIG)、NUL 引數為 `error` | `MANIFEST`(provider `codex-cli`、single、`effort_map` none→minimal、max→xhigh)、`create(context)`(容忍 `cli_runner=None`) | 7.4、5.2.3、5.3.1、6.3 |
| 存取 token(`settings`)與 `create_app(issue_token)` | token 產生、雜湊與寫入(D30) | `settings.generate_access_token()`、`hash_access_token(token)`、`get_access_token_hash(conn)`、`rotate_access_token(conn) -> str`、`ensure_access_token(conn) -> str \| None`(同一 `BEGIN IMMEDIATE` 交易內檢查與寫入);`app.create_app(..., issue_token=False)`:只有 `spillway serve` 傳 True,lifespan 經 `_ensure_token(app)` 在沒有雜湊時產生並以單行印到 stderr | 8.1.3、9.2 |
| `Attempt` | Router 交給 LedgerWriter 的單次嘗試記錄:ts、route_name、deployment_id、effort、tokens、cost、status、slot、fallback_depth、latency_ms、est_*、error、請求與回應內容、`extra_secrets` | 型別,定義於 `ledger.py` | 2.4、4.5.1 |
| `Plan` | dry-run 結果:slots(每個候選含 deployment、kind、weight、effort、`Estimate`、健康狀態)、`excluded[]`(deployment、原因,含 `adapter_unavailable`、`skipped_precheck`)、`unsorted[]`、依 Rng 本次抽出的第一順位 | 型別,由 `Router.dry_run` 回傳 | 4 |
| `LedgerWriter` | 每次嘗試一列(呼叫後與結果同列一次寫入,D24);payload 先遮罩再截斷 | `record(attempt) -> ledger_id`;`payload_status(ledger_id, now) -> ok \| expired \| never_recorded` | 2.4、4.5.1 |
| `RetentionJob` | 每小時刪除過期 payload | `run_once(now)` | 2.4.5 |
| `Redactor` | 遮罩 `sk-…`、`Bearer` 後值、`extra_secrets` 中的每個值;無狀態,全站共用單一實例 `spillway.redact.REDACTOR`(`extra_secrets` 每次呼叫傳入;`LedgerWriter(redactor=None)` 預設用它,測試仍可傳入自己的實例;CHG-20261101-02 決策 7) | `redact(text, extra_secrets=()) -> (text, hit: bool)`;`matches(text) -> bool`(供寫入驗證) | 2.4.4 |
| `ConfigIO`(M4) | YAML ↔ 資料庫 | `export() -> str`;`import_(yaml, mode)`,失敗回欄位路徑 | 3 |
| `McpConfigRenderer`(M4) | 模板渲染各工具設定 | `render(group, tool, embed_token) -> {content, path, env}` | 8.2 |
| `McpApp`(M3) | 依 `<group>` 暴露 route;list_changed | MCP tools `ask`、`list_routes`、`quota_status` | 8.1 |
| `Clock` / `Rng` | 時間與亂數來源;實作:`SystemClock`、`FakeClock`(`advance()`)、`SystemRng(seed=None)`、`SeqRng` | `now() -> datetime(UTC)`;`random() -> float` | 測試注入 |
| `domain` 值物件 | `Deployment`、`Budget`、`ChatRequest`、`CallResult` 定義於基礎層 `spillway/domain.py`;`adapters.base` 轉匯出,adapter 模組照舊只 import base | dataclass(frozen) | 7.1;CHG-20261030-02 施工審議 M1 |
| `probe_http` | local 端點健康探測(D18):對 URL 發一次 GET,任何 HTTP 回應即健康 | `async probe_http(url, timeout=2.0) -> bool`(不外拋、不跟隨轉址、不讀 proxy 環境變數) | 4.2.4;CHG-20261030-02 施工審議 M8 |
| `LiteLLMApiAdapter`(`adapters.base.api_adapter`) | anthropic_api、openai_api、litellm 三個模組共用的 adapter 基底(行為與去重前的 adapter.py 相同);各模組 `adapter.py` 保留,內容為 `class LiteLLMAdapter(LiteLLMApiAdapter)`,不覆寫 `call`/`health`,`__init__.py` 不變。**`_seam` 替換點規則**(CHG-20261101-02 決策 11,施工審議修訂):`litellm_call`、`model_string`、`probe_http` 於呼叫當下先查實際類別(`type(self)`)定義所在的那一個模組(只查一層,不沿繼承鏈往上找),該模組有同名屬性且可呼叫就用它,否則退回 `adapters.base` 的實作;各模組 `adapter.py` 轉匯出這三個名稱,所以在某模組替換它們只影響該模組 | `LiteLLMApiAdapter(context, manifest)`;`async call(dep, req, effort) -> CallResult`;`async health(dep) -> bool` | 7.1、7.2 |
| `model_string` / `load_litellm` | 統一的 LiteLLM 模型字串規則(provider 為 `litellm` 時原樣使用 model,其他一律 `<provider>/<model>`,spec 7.2.1);LiteLLM 單一載入點(本地價格表、關閉 verbose 與遙測、不下載 HF tokenizer;`LiteLLM`、`LiteLLM Router`、`LiteLLM Proxy`、`openai`、`anthropic`、`httpx`、`httpcore` 七個 logger 等級低於 INFO〔含 NOTSET、DEBUG、自訂 1–19〕時設為 INFO,INFO 以上與 propagate、handler 不動,CHG-20261101-02 決策 13) | `model_string(dep) -> str`;`load_litellm() -> module` | 7.2.1;CHG-20261030-02 施工審議 M11、M13、M14 |
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
  Router --> HealthCache : invalidate_health
  Router --> CliRunner : cli_attempt(名額、extra_env_remove)
  CandidateFilter --> CliRunner : in_flight
  CandidateFilter --> HealthCache
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
  Adapter <|.. LiteLLMApiAdapter
  LiteLLMApiAdapter <|-- LiteLLMAdapter : 三個 API 模組各一
  Adapter <|.. ClaudeCliAdapter
  Adapter <|.. CodexCliAdapter
  AdapterRegistry --> AdapterManifest : discover()
  ClaudeCliAdapter --> CliRunner : run()
  CodexCliAdapter --> CliRunner : run()
  ClaudeCliAdapter --> messages_to_prompt
  CodexCliAdapter --> messages_to_prompt
  ClaudeCliAdapter --> classify
  CodexCliAdapter --> classify
  LiteLLMApiAdapter --> classify
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
├── adapter.py       # 實作 Adapter Protocol(API 模組為 `class LiteLLMAdapter(LiteLLMApiAdapter)`,CHG-20261101-02 決策 11)
├── models.yaml      # 可用模型清單(D12),純資料,改清單不必改程式
└── README.md        # 這個 AI 的接法、所需 CLI/環境變數、已知限制
```

`__init__.py` 必須提供:

| 名稱 | 內容 |
|------|------|
| `MANIFEST: AdapterManifest` | `providers`(此模組處理的 `provider` 值,如 `["claude-cli"]`)、`kinds`(`cli`/`api`/`local`)、`effort_map`(五級 → 上游值或 `None`,取代 spec 6.3 的共用表)、`default_signals`(`{quota, rate_limit}` 正規式,spec 5.3.1 預設)、`default_capabilities`、`default_params`(如 `timeout`、`effort_args`)、`requires`(需要的執行檔或環境變數,用於健康檢查與 UI 提示)、`fallback_for_kinds`(只有 litellm 模組宣告 `["api","local"]`)、`access`(`subscription-cli` / `api` / `local`,見 D14)、`cardinality`(`single` = 此 provider 全系統只能有 1 個成員;`multiple` = 可建多個,見 D15)、`contract_version`(目前 `1`);另可選 `list_models(context)`,執行期向上游查詢可用模型(例:local 端點的 `/v1/models`) |
| `create(context) -> Adapter` | 建立實例;`context` 為 `AdapterContext`,欄位只有 `credentials` 解析器、`clock`、`cli_runner`(預設 None;M3a 起 app 注入 `CliRunner`)、`data_dir`(預設 None;M3a 起 app 注入資料庫所在目錄)——模組拿不到 `store`、`routing`(決策見 CHG-20261030-02 決策 13)。CLI 模組的 `create()` 必須容忍 `cli_runner=None`(此時 `call` 回 `error`) |

規則:

1. **新增一種 AI = 新增一個子套件資料夾**,不修改任何既有檔案(含 registry)。spec 7.1.2「只可新增 adapter 檔案與註冊一行」的上限因此收緊為零行。
2. 模組只可 import `spillway.adapters.base`(Protocol、`CallResult`、`AdapterManifest`、`AdapterContext`、`classify`、`effort_to_upstream`、`litellm_call`、`model_string`、`probe_http`、`LiteLLMApiAdapter`;M3a 起另有 `CliRunner`、`CliOutput`、`messages_to_prompt`)、標準庫與 `pyyaml`;不可直接 import `litellm`、`spillway.credentials`、`spillway.clock`;**不可 import `routing`、`store`、`api`、`mcp`**。**不可自行起子行程**(CHG-20261101-03 Global Constraints):不可 import `subprocess`、`pty`、`asyncio.subprocess`、`multiprocessing`(含子模組);不可使用 `os`/`posix` 上的 `system`、`popen`、`spawn*`、`exec*`、`posix_spawn*`、`fork`、`forkpty`;不可使用任何物件上的 `create_subprocess_*`、事件迴圈的 `subprocess_exec`/`subprocess_shell`、`ProcessPoolExecutor`(以別名 import 繞過同樣違規);`import asyncio`、`tempfile`、`os` 本身仍允許。CLI 模組只能經 `AdapterContext.cli_runner` 起子行程。由 `tests/contract/test_layering.py` 以 AST 檢查(禁用清單在 `adapters/base/contract.py` 的 `layering_violations`)。
3. 每個模組必須通過共用合約測試(對所有被發現的模組參數化執行):`tests/contract/test_adapter_contract.py` 檢查 manifest 欄位齊全且合法、`effort_map` 五級都有定義、`models.yaml` 可解析;`tests/contract/test_call_status.py` 檢查 `call` 對假上游的成功/用完/429/錯誤四種輸出回傳正確的 `CallResult.status`(adapter 在 `call()` 內以 `classify` 歸類)。新模組另放自己的假上游於 `tests/fakes/<module>/`;三個內建 API 模組共用 `tests/fakes/api_upstream/`(D17)。repo 外的模組(以 `discover(extra_paths)` 載入)以環境變數 `SPILLWAY_CONTRACT_EXTRA_PATHS` 納入合約測試,其 call_status 測試 harness 以 `SPILLWAY_CONTRACT_HARNESS_PATHS` 目錄下的 `<module>_harness.py` 提供(CHG-20261030-02 決策 57)。模組層級的 `list_models(context, …)` 可為同步或 async 函式。
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
  | `not_found` | 404 | api.admin;全站 | 資源不存在、payload 從未記錄(D22),或任何未知路徑(全站 handler) |
  | `conflict` | 409 | api.admin | 例如刪除仍被 route 參照的群組(D22) |
  | `internal_error` | 500 | api(全站) | 非預期例外(例如帳本寫入失敗,決策 35);message 只含例外型別(CHG-20261030-02 決策 42) |
  | `method_not_allowed` | 405 | api(全站) | HTTP 方法不允許(決策 42) |
  | `http_error` | 其他 | api(全站) | 其餘 HTTPException 的後備(決策 42) |

## 設計決策與取捨

| # | 決策 | 選項 | 取捨理由 |
|---|------|------|----------|
| D1 | 上游呼叫用 async | async(`litellm.acompletion`、`asyncio.create_subprocess_exec`) vs sync + threadpool | CLI 呼叫可達 600 秒;async 不佔執行緒,並行上限用不排隊的名額計數(`CliRunner.try_acquire`/`release`,見 D29)直接表達「滿了視為不可用」(NFR-6)。 |
| D2 | SQLite 用標準庫 `sqlite3`,短交易、每請求一連線 | `sqlite3` vs `aiosqlite` vs ORM | 單人寫入量小、查詢皆 ms 級;WAL 允許讀寫並行。ORM 對 11 張以內的表收益低,且 DDL 已在規格書定案。 |
| D3 | Schema 版本以 `PRAGMA user_version` 管理,啟動時依序套用 migration | user_version vs Alembic | 不引入 ORM 就不需要 Alembic;單檔資料庫用 user_version 足夠。 |
| D4 | 時間與亂數一律注入(`Clock`、`Rng`) | 注入 vs `freezegun` 等 monkeypatch | AC-4、AC-14、AC-17、NFR-5 需要「推進系統時間」與可重現分布;注入比全域 patch 穩定,也能用在背景清理工作。 |
| D5 | 假上游(Guideline Q1):`tests/fakes/cli_upstream/` 產生假的 `claude`、`codex` 執行檔(以 `sys.executable` 寫成的 wrapper,避開 macOS `/usr/bin/python3` stub),測試時放到 `PATH` 最前面;API 的假上游見 D17(位於 `tests/fakes/api_upstream/`) | PATH 注入 vs 在成員設定加「指令路徑」欄位 | PATH 注入不需要改 spec 7.3.1 的命令定義。CHG-20261101-03 決策 18 定案:情境由控制檔 `$SPILLWAY_HOME/fake_cli/<claude\|codex>.json` 切換(每次呼叫重讀,長駐的 serve 也能切換;`scenario` 為 ok、quota、rate_limit、error、timeout、non_json、hold_until_release、grandchild_holds_stdout,另有 `delay_seconds`),每次呼叫寫一個記錄檔(argv、stdin、cwd、`SPILLWAY_DEPTH`、指定變數是否存在,不寫值)。真實 CLI 護欄(決策 19)放在 `tests/conftest.py`(session 起始即清理 PATH,移除解析到真實 claude/codex 的目錄與空、相對、`~` 開頭的項目)、`tests/features/environment.py` 與測試啟動的 serve 子行程環境三處,找到真實執行檔即失敗。API 的 mock 回應 vs 本機假 HTTP 端點在 CHG-20261030-02 定案為後者(D17)。 |
| D6 | MCP 用官方 `mcp` Python SDK 的 low-level server,單一 ASGI app 掛在 `/mcp/{group}`,依 path 參數決定暴露的 route | 低階 server vs 每群組一個 FastMCP 實例 | 群組會在執行期新增刪除;每群組一個實例要動態掛載與拆除,低階 server 依請求查表較單純,也方便發 `list_changed`。 |
| D7 | Web UI:Jinja2 伺服端渲染 + 原生 JavaScript(HTML5 拖放、`fetch` 呼叫 `/api/*`),不引入前端建置與框架 | 原生 JS vs htmx vs SPA | 企劃書已定「不引入前端建置流程」;7 個畫面中只有 UI-4 拖曳需要較多互動,原生 DnD 足夠。 |
| D8 | 資料目錄:環境變數 `SPILLWAY_HOME`,未設時用 `platformdirs.user_data_dir("spillway")`;內含 `spillway.db`(與 WAL 的 `-wal`、`-shm`)、`spillway.lock`(D28)、升級備份 `spillway.db.v1.bak[.<UTC 時間戳記>]`(D28)、CLI 模組未設定 `params.cwd` 時的工作目錄 `work/claude/`、`work/codex/`(`spillway init` 建立,模組在不存在時也會建立) | 固定 `~/.spillway` vs platformdirs | 規格書只說「閘道資料目錄」;platformdirs 在三平台各自落在慣例位置(NFR-7)。**使用者確認**(2026-10-30)。原列的 `tmp/` 未實作:codex 的 `-o` 暫存檔用系統暫存目錄(`tempfile.mkstemp`,0600,呼叫後刪除;CHG-20261101-03 決策 11)。 |
| D9 | 預設埠號 8787、綁定 127.0.0.1;存取 token **M3 起**於 `spillway init` 或首次啟動產生,只印一次,資料庫存 SHA-256;M2 不產生也不驗證 token,管理 API 在 M2 無驗證(前提為只綁 127.0.0.1) | (a) 只綁本機、token 延 M3 / (b) M2 完整做 token / (c) 先產生 token 不驗證 | 依 spec 8.1.3、9.1.1 與範例;8787 取自規格書範例。**使用者確認**埠號(2026-10-30)。CHG-20261030-02 依**使用者決定 B1**(2026-10-30)修訂:原文為「token 於 init 或首次啟動產生」,改為 M3 起;spec 9.1.1 只在綁定其他位址時要求 token。M3a(CHG-20261101-03)已實作 token 的產生與 rotate,見 D30;驗證於 M3b 的 `/mcp/*`。M3 仍只允許綁 127.0.0.1(D16 不放寬)。 |
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
| D25 | OpenAI 舊版參數 `functions`、`function_call` 依 U8 白名單丟棄:`/v1/chat/completions` 只轉交 `spillway.api.ALLOWED_CHAT_PARAMS` 內的參數,名單外的鍵(含這兩個)靜默丟棄、不報錯;工具呼叫請用 `tools`/`tool_choice`。此事寫入 README「相容性」小節 | README + design vs spec 9.1 修訂 | spec 9.1 未列舉白名單;白名單是實作細節(U8 = a,使用者決定 2026-10-30) | 不動規格書。**代使用者決定**(CHG-20261101-02 決策 15)。 |
| D26 | 管理 API 寫入時的憑證鍵名比對改為「正規化後比對」:鍵名轉小寫並去掉 `-`、`_`、`.`、空白後,(a) 完全等於 `apikey`、`apikeys`、`xapikey`、`key`、`token`、`secret`、`password`、`passwd`、`passphrase`、`authorization`、`bearer`、`credential`、`credentials` 之一,或 (b) 以複合字根 `apikey`、`apikeys`、`secret`、`password`、`passwd`、`passphrase`、`secretkey`、`privatekey`、`accesskey`、`apitoken`、`accesstoken`、`authtoken`、`sessiontoken`、`refreshtoken`、`idtoken`、`bearertoken` 之一結尾 → 422 `validation_error`,不回顯值(`api.admin.secrets`);`key`、`token` 只做完全比對,不擋 `max_tokens`、`cache_key`。整棵 `params` 含 `params.signals` 一律比對鍵名與值樣式、**不豁免**;寫入 `params` 時另以 `validate_signals(params["signals"], "params.signals")` 驗證格式,不合法回 422;`signals` 為 null 或未給視為不覆寫模組預設,不驗證 | (a) 擋變體 / (b) 維持完全比對;複合字根 (a) 不加 / (b) 加 / (c) 再加 `key` 結尾;signals (a) 不豁免 / (b) 只豁免結構層 / (c) 整棵豁免 | 原規則(D22、U9)只擋 8 個完全相同的鍵名;誤擋的代價只是一次明確的 422;spec 5.3.1 的結構鍵沒有一個會被擋到 | 已知限制:「供應商名 + key/token」(`openai_key`、`hf_token`)不擋;`match`/`equals` 若要比對名稱就叫 `token`、`secret` 的上游欄位會 422。**使用者決定**(2026-11-01,CHG-20261101-02 決策 1、4,含決策審議 B1、B2)。 |
| D27 | 連線取得統一:基礎層 `store.db.ConnSource`(`sqlite3.Connection` 或零參數 callable)與 `store.db.use_conn(source)` context manager 取代 resolver、quota、estimate 各自的 `_conn()`;**`use_conn` 一律不關閉連線**(傳入 Connection 直接交回;傳入 callable 呼叫一次並交回),關閉責任留在開啟者(Router 的 `close_conn`、API 層的 `request_conn`)。API 層以 `api.deps.request_conn(request)` context manager 取得連線:每次呼叫讀 `request.app.state.conn_factory()` 開一條、離開 `with` 時關閉(含例外),在 async handler 內於事件迴圈執行緒執行;不使用 FastAPI `Depends`。SQL 字串只出現在 `src/spillway/store/`(`tests/contract/test_layering.py` 檢查) | `Depends(get_conn)` vs context manager vs 保留現狀 | 連線以預設 `check_same_thread=True` 開啟,同步 generator dependency 會在 threadpool 開連線;既有測試以 `SimpleNamespace(app=app)` 直接呼叫 handler、或在建 app 後替換 `conn_factory` | context manager 與原 admin `_db(request)` 開關時機相同(回應建立前關閉)、執行緒不變。Router 的 contextvar 不動(NFR-1 依賴它);`LedgerWriter`、`RetentionJob` 維持收 Connection。**代使用者決定**(CHG-20261101-02 決策 5)。 |

| D28 | schema v2 與 migration(CHG-20261101-03 決策 3、4、5):冷卻獨立成 `cooldowns(deployment_id PK → deployments ON DELETE CASCADE, exhausted_until NOT NULL, reason CHECK quota_exhausted/rate_limited)`,`budgets` 移除 `exhausted_until`;migration 於開啟資料庫時自動執行(`serve`、`init`)。前置條件與順序:① serve 以暫時 socket 試綁埠(設 `SO_REUSEADDR`)後關閉,失敗即退出、不開資料庫;② 非阻塞取得 `spillway.lock`(`fcntl.flock`,同行程可重入;serve 持有到行程結束);③ 唯讀 `PRAGMA foreign_key_check`,舊庫已有違反時不備份、不升級;④ SQLite backup API 複製為 `spillway.db.v1.bak`(含未 checkpoint 的 WAL;複製後設 `journal_mode=DELETE` 成為單檔;已存在加 UTC 時間戳記後綴、不覆寫;失敗刪殘檔、不升級);⑤ `BEGIN IMMEDIATE` 後重讀版本,單一交易內建表、搬資料(`reason` 由帳本推得)、`DROP COLUMN`、`user_version=2`,完成後 `foreign_key_check` 必須為空;失敗回滾、訊息含備份位置。空庫不需鎖與備份。不刪除任何 `budgets` 列(含決策 59 暫行列)。API 形狀:成員與總覽回應頂層 `cooling_until`、`cooling_reason`,只在未到期時有值(spec 9.2.4) | (a) 新表 / (b) `deployments` 加欄;自動 migration + 備份 vs 只以明確命令執行 | 冷卻是執行期狀態、額度是設定,M4 匯出入只處理設定;擁有者的冒煙資料庫有真實資料,新 serve 若在舊 serve 仍執行時 migrate 會讓舊 serve 失效,所以先檢查埠與鎖;外鍵預檢放在備份前,否則每次啟動都多一份必然失敗的備份(施工審議 T1)。回滾:停 serve、刪除 `spillway.db`、`-wal`、`-shm`,把 `.v1.bak` 改名為 `spillway.db`、回退程式版本;v2 期間寫入的資料遺失。**使用者決定**(2026-11-01,決策 3、5);決策 4 為代使用者決定。 |
| D29 | CLI 子行程規則與併發名額協定(CHG-20261101-03 決策 7、8、9、10、11、12a):子行程一律經 `CliRunner`(argv 陣列、不經 shell、獨立 process group、逾時/取消/停機殺整個 group);環境逐模組移除固定清單(見「關鍵元件」表的 `ClaudeCliAdapter`、`CodexCliAdapter` 列)並設 `SPILLWAY_DEPTH=1`;官方登入變數照常繼承,src 不出現 OAuth 變數名;隔離旗標由模組固定附加、`extra_args` 無法移除(claude `--strict-mcp-config`;codex `-c mcp_servers={}` 為 `--` 前最後一個 `-c`,`extra_args` 拒絕 `--` 與裸位置引數)。**共用守門 `Router.cli_attempt(dep, conn, *, fallback)`**,路由與 `POST /api/deployments/{id}/test` 都經它:① `shutting_down` 為真 → 拒絕 `shutting_down`;② `cli_runner.try_acquire(dep.id, max_concurrency(dep))` 失敗 → 拒絕 `concurrency_full`;③ 由全部成員(含停用者)`credential_ref` 為 `env:<名>` 的變數名設 `cli_runner.extra_env_remove`,這些變數目前的值作為該次嘗試的 `extra_secrets`(Redactor 遮罩帳本、payload 與錯誤訊息);離開時(含例外與 `CancelledError`)`release`。拒絕時不呼叫 adapter、不寫帳本、不算 fallback 深度、不設冷卻(test-call 回拒絕結果)。不變式:進入守門到 adapter 呼叫 `cli_runner.run()` 組環境之間不可有 await(單一事件迴圈下名額不會被搶、`extra_env_remove` 不會被改)。CandidateFilter 先以 `in_flight` 篩除已滿者;「篩選後被搶走」的嘗試不寫帳本,是 spec 4.5.1「每次嘗試一列」的明列例外 | 篩除不寫帳本 vs 帳本記 error vs 新狀態;名額由 Router 取得 vs 模組自取 | NFR-6「超過視為不可用,不排隊」;模組不碰名額、不讀成員清單(分層);test-call 原本直呼 adapter 會繞過名額、env 移除、遮罩與停機旗標(施工審議 T6),改經同一守門讓兩條路徑一致。已知限制:CLI 子行程仍繼承清單以外的環境變數,Redactor 只遮罩已知樣式與成員憑證值;codex prompt 在 argv(`ps` 可見,超過 ARG_MAX 回 `error`);`-c mcp_servers={}` 是取代或合併、claude plugin 與 claude.ai connector 的 MCP 是否被 `--strict-mcp-config` 排除,由擁有者冒煙確認;真實 CLI 輸出格式以本機 claude 2.1.308、codex 0.163.0 確認。**使用者決定**(決策 7、11);其餘代使用者決定。 |
| D30 | 存取 token(CHG-20261101-03 決策 14、15、17):`secrets.token_urlsafe(32)`,`settings.access_token_sha256` 只存 SHA-256 十六進位;明文只出現一次——`spillway init` 的 stdout(單獨一行,其他提示一律走 stderr)、serve 首次啟動的 stderr 一行、`POST /api/settings/token/rotate` 回應 `{"token": "..."}`。`spillway init`:建立資料目錄與 `work/claude`、`work/codex`,在 `spillway.lock` 內開啟資料庫(migration 含 D28 的備份;不做埠檢查)並產生 token;已存在時於 stderr 印「已存在,未變更」並回 0(冪等),`--rotate` 才重新產生;鎖被占用或 migration 失敗回 1(`EXIT_STARTUP_FAILED`)。`create_app(issue_token=False)`:只有 serve 傳 True(既有測試要求 lifespan 後 settings 表為空,施工審議 T7)。M3a 不以 token 驗證任何請求 | init 或首次啟動 vs 只有 init;init 冪等 vs 每次重新產生 | spec 8.1.3、D9;冪等讓 `TOKEN=$(spillway init)` 重跑不會讓既有 token 失效。已知限制:本機其他程式可對 127.0.0.1 呼叫 rotate 取得有效的新 token(M3b 的 `/mcp/*` 驗證可經 `/api` 繞過),網頁也可經跨站 POST 讓現有 token 失效——Host/Origin 檢查留到 M3b 與 `/mcp` 一起定;擁有者把 serve 的 stderr 導到檔案時,檔案中會有一次明文。**使用者決定**(決策 14);其餘代使用者決定。 |
| D31 | graceful shutdown 機制(CHG-20261101-03 決策 16;`cli._graceful_server`):`uvicorn.Server` 子類別。① 第一個 SIGTERM/SIGINT 先設 `app.state.shutting_down = True`(Router 不再開始新嘗試、不起子行程;嘗試之間開始停機時其餘成員記 `shutting_down`),再交給 uvicorn 原本的 `handle_exit`;② uvicorn 停止接新連線,等待進行中請求至多 `timeout_graceful_shutdown`(`_run_server(graceful_timeout=30)`),期滿取消請求 task——CliRunner 在取消路徑殺 process group,Router 的取消路徑在停機中寫帳本 `status=error`、`error="shutdown"`(否則 `cancelled`);uvicorn 0.54 期滿取消後不等 task 就跑 lifespan 結束,所以覆寫 `shutdown` 在 lifespan 結束前有限時間(SIGTERM 寬限 5 + 讀管線 2 + 1 = 8 秒)等待被取消的 task;③ lifespan 結束時 `terminate_all(grace=5)` 為後備;④ 轉為 `force_exit`(停機中再收到 SIGINT)時以 `call_soon_threadsafe` 立即取消全部請求 task(CliRunner 立即 SIGKILL);覆寫 `capture_signals` 在 uvicorn 重送訊號**之前**同步 `kill_all_sync()`;`_run_server` 的 `finally` 與 `atexit` 也呼叫它。依賴的 uvicorn 0.54.0 內部行為(`server.py`:`handle_exit` 設 `should_exit`/`force_exit`、`shutdown()` 期滿取消 `server_state.tasks` 但不等待、`force_exit` 略過 `lifespan.shutdown()`、`self.lifespan` 於 `_serve` 建立、`capture_signals()` 離開時還原處理器並以相反順序 `signal.raise_signal` 重送)以 `tests/integration/test_graceful_shutdown.py` 的子行程測試為絆線,升級 uvicorn 時必須重跑。環境變數 `SPILLWAY_TEST_GRACEFUL_TIMEOUT` **僅供測試**縮短等待上限,正式使用不設定 | (a) 等 30 秒再終止 / (b) 等到完成 / (c) 立即終止 | 原機制(只靠 lifespan)無效:uvicorn 先取消請求 task 再跑 lifespan,且終端 Ctrl-C 會打到同一 process group 的子行程——子行程因此自成 group,清理改由取消路徑負責。施工審議 T8:SIGTERM 後再 SIGINT 時 force_exit 略過 lifespan,uvicorn 依序重發的 SIGTERM 為預設處置、行程立即結束,`finally`/`atexit` 不執行,所以必須在 `capture_signals` 內同步殺子行程。已知限制:force_exit 不保證寫入該次 `error="shutdown"` 帳本列;子行程自行 `setsid` 脫離 group 時無法被殺;停機期間的新請求在真實 serve 上是連線被拒(監聽 socket 已關),503 `shutting_down` 只在行程內測試驗證;**M3b 必須就 `/mcp` 長連線重新檢查 30 秒上限**。**使用者決定**(2026-11-01,選 (a));機制依決策審議與施工審議改寫。 |
| D32 | (補錄 CHG-20261101-02 決策 14)payload 清理排程 `RetentionScheduler.due()` 的提前量為 `2 × poll`(poll 預設 30 秒、interval 3600 秒):輪詢抖動 ≤ poll 時,兩次清理間隔 ≤ interval;清理最多提早 2 × poll | 2×poll vs 以單調時鐘排程 | 抖動來自 `asyncio.sleep` 超時與清理耗時,量級遠小於一個 poll;改一個常數即可把 NFR-5 上界拉回 1 小時內,提早 60 秒不違反「每小時」。已知限制:單次清理耗時超過 poll 不在上界內。**代使用者決定**(CHG-20261101-02 決策 14)。 |

## 採用模式

- **Strategy + 自動發現的 Registry**:`Adapter` 介面 + 每模組一份 manifest;新增供應商只加一個子套件資料夾(D11)。
- **Pipeline**:`Router` 內部是 Resolver → Filter → Planner → Selector 的固定管線,每段可單獨測試,也直接對應 dry-run 要回傳的中間結果。
- **設定驅動**:CLI 參數(`effort_args`、`extra_args`)、上游訊號規則(`signals`)、MCP 工具格式(模板檔)都是資料,不寫死(企劃書第九節風險對策)。
- **Ports & adapters 的測試縫**:`Clock`、`Rng`、`tests/fakes/api_upstream/` 的假 API 端點(M2);`CliRunner` 加 PATH 上的假 CLI 與控制檔(M3a,D5),讓自動測試不碰真實上游(Guideline Q1)。

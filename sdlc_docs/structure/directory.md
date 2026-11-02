# 目錄結構 — Spillway

- 專案:spillway · 分支:main · 版本:v1.4(2026-11-02,CHG-20261102-01:`api/mcp/`(含 `sdk_compat.py`、`bridge.py`)、`api/host_origin.py`、`api/drain.py`、`api/admin/mcp_config.py`、`templates/mcp/`、`quota.py` 的 `member_quota_rows`、`adapters/base/cli_common.py` 與 `adapters/base` 全部檔名、`tests/helpers/` 套件與 `live_gateway.py`、`tests/integration/mcp/`、`m3b_mcp.feature` 與 `m3b_steps.py`、`artifacts/m3b/`;樹狀與 `git ls-tree` 對齊;使用者於 CHG 確認閘確認,2026-11-02) · 前版 v1.3 已確認(2026-11-01,CHG-20261101-03:`adapters/base/cli_runner.py`、`cli_prompt.py`、`adapters/claude_cli/`、`adapters/codex_cli/`、`tests/fakes/cli_upstream/`、`tests/fakes/claude_cli/`、`tests/fakes/codex_cli/`、`m3a_cli.feature` 與 `m3a_steps.py`、`artifacts/m3a/`、資料目錄內容;使用者於 CHG 確認閘確認,2026-11-01) · 前前版 v1.2 已確認(2026-11-01,CHG-20261101-02:`api/admin/` 套件、`api/deps.py`、`routing/outcome.py`、`adapters/base/api_adapter.py`、`tests/helpers.py`、`m2c_cleanup.feature` 與 `cleanup_*_steps.py`、`artifacts/m2c/`;移除 tests/contract、tests/unit/adapters、tests/unit/adapters_base、tests/unit/routing 的 conftest;使用者於 CHG 確認閘確認,2026-11-01) · v1.1 已確認(2026-10-30,CHG-20261030-02) · v1.0 已確認(2026-10-29,使用者) · 日期:2026-11-02(UTC+0)
- 依據:sdlc_docs/structure/logical.md、design.md

## 樹狀

```
spillway/
├── AGENTS.md                    # AI 入口
├── README.md
├── .gitignore                   # .venv/、__pycache__/、*.egg-info/、.pytest_cache/、artifacts/
├── pyproject.toml               # 套件、相依(M3b 明列 mcp>=1.30,<2、jinja2>=3、sse-starlette>=3.5.0)、console script `spillway`、package data(含 templates/mcp/*.j2)
├── docs/                        # 企劃書、規格書、UI 設計稿(需求來源,不放帳本)
├── sdlc_docs/                   # 治理帳本
├── artifacts/                   # 驗收與量測產物(不進版控,M2 起)
│   ├── m2/                      # CHG-20261030-02 的驗收產物
│   ├── m2c/                     # CHG-20261101-02 的驗收產物(integration.xml、contract.xml、http-log.jsonl)
│   ├── m3a/                     # CHG-20261101-03 的驗收產物(integration.xml、contract.xml、http-log.jsonl:只含 method、path、status、elapsed_ms)
│   └── m3b/                     # CHG-20261102-01 的驗收產物(integration.xml、cli.xml、contract.xml、http-log.jsonl〔四鍵,含 MCP 請求〕、operation-results.json)
├── src/spillway/
│   ├── __init__.py
│   ├── __main__.py / cli.py     # spillway serve(M2;M3a 加 _preflight 與 graceful shutdown;M3b 停用 sse-starlette auto-drain、shutdown_hooks、drain_expired、Ctrl-C 結束碼 130)、init [--rotate](M3a)、mcp-stdio --group <g> [--port](M3b)
│   ├── app.py                   # FastAPI 組裝 create_app(issue_token=False, port=8787)、最外層 HostOriginMiddleware、/mcp 掛載、lifespan(背景清理工作、CliRunner 注入、token、MCP session manager 持有 task、D31 關閉順序、terminate_all 後備)
│   ├── settings.py              # 資料目錄、埠號、綁定常數;存取 token 產生與雜湊讀寫(M3a,經 store.repo)
│   ├── clock.py                 # Clock、Rng
│   ├── domain.py                # 跨層值物件 Deployment、Budget、ChatRequest、CallResult(adapters.base 轉匯出);共用常數 KINDS、CLOUD_KINDS、CALL_STATUSES、LEDGER_STATUSES、COOLING_STATUSES、STATUS_SKIPPED_PRECHECK
│   ├── store/
│   │   ├── __init__.py
│   │   ├── db.py                # 連線、PRAGMA、migration(user_version;v1→v2 的外鍵預檢、備份、交易內升級)、資料目錄鎖 spillway.lock、ConnSource / use_conn(不關閉連線)
│   │   ├── schema.sql           # schema v1 基線(新庫先套 v1);v2 由 db.py 的 migration `_SCHEMA_V2` 套用,migration 後結果與 spec 2.3 一致
│   │   └── repo.py              # 各表存取(全專案的 SQL 只在 store/;冷卻函式讀寫 cooldowns)
│   ├── credentials.py
│   ├── redact.py                # Redactor、共用實例 REDACTOR
│   ├── routing/
│   │   ├── __init__.py          # 公開匯出(Router、OrderPlanner、members_of、RouteOutcome 等)
│   │   ├── target.py            # route 目標(group / tags)→ 成員集合(members_of)
│   │   ├── resolver.py
│   │   ├── filter.py            # CandidateFilter、HealthCache(invalidate)
│   │   ├── planner.py
│   │   ├── selector.py
│   │   ├── outcome.py           # RouteOutcome、AttemptInfo、Plan、PlanSlot、PlanEntry、PlanExcluded
│   │   └── router.py            # Router(invalidate_health)
│   ├── quota.py                 # QuotaService;member_quota_rows、is_cooling_only(M3b:/api/dashboard 與 MCP quota_status 共用;上游訊號判定 `classify` 在 adapters/base/)
│   ├── estimate.py
│   ├── effort.py                # EffortMapper、normalize_effort、InvalidEffort
│   ├── ledger.py                # LedgerWriter、RetentionJob
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── base/                # 共用契約(模組只可 import 這一層)
│   │   │   ├── __init__.py      # 公開匯出
│   │   │   ├── protocol.py      # Adapter Protocol、AdapterContext
│   │   │   ├── types.py         # 轉匯出 domain 值物件(CallResult 等)
│   │   │   ├── manifest.py      # AdapterManifest 與驗證
│   │   │   ├── registry.py      # AdapterRegistry(自動發現)
│   │   │   ├── signals.py       # classify(上游訊號歸類)
│   │   │   ├── effort.py        # effort_to_upstream
│   │   │   ├── litellm_call.py  # litellm_call、model_string、load_litellm
│   │   │   ├── api_adapter.py   # LiteLLMApiAdapter(三個 API 模組共用的基底)
│   │   │   ├── probe.py         # probe_http(local 健康探測)
│   │   │   ├── cli_runner.py    # CliRunner、CliOutput、cli_timeout(M3a)
│   │   │   ├── cli_prompt.py    # messages_to_prompt(M3a)
│   │   │   ├── cli_common.py    # cli_cwd、head、tail、nonneg_int、CLI_WORK_SUBDIRS(M3b,CHG-20261102-01 決策 20)
│   │   │   └── contract.py      # 分層與子行程禁用規則
│   │   ├── litellm/             # 通用後備(api/local);每個模組:__init__.py(MANIFEST、create)、adapter.py、models.yaml、README.md(API 模組的 adapter.py 為 LiteLLMApiAdapter 子類別)
│   │   ├── claude_cli/          # 訂閱 CLI · single(M3a):claude -p
│   │   ├── codex_cli/           # 訂閱 CLI · single(M3a):codex exec
│   │   ├── anthropic_api/       # API · multiple
│   │   └── openai_api/          # API · multiple
│   ├── api/
│   │   ├── __init__.py          # ApiError、錯誤格式工具(error_response、redact_echo)、ALLOWED_CHAT_PARAMS(U8 白名單)
│   │   ├── deps.py              # request_conn(每次開一條連線、離開時關閉)
│   │   ├── openai.py            # /v1;Router.route 經 run_until_drain(停機期滿 503 shutting_down)
│   │   ├── host_origin.py       # HostOriginMiddleware(全端點 Host/Origin,純 ASGI;M3b,design D33)
│   │   ├── drain.py             # run_until_drain、Drained、ShutdownHooks、CANCELLED_DRAIN_TIMEOUT(M3b,design D31)
│   │   ├── admin/               # 管理 API 套件:__init__.py(組 router)、common.py(含 tools_changed)、models.py、secrets.py、deployments.py、groups.py(群組名 all 保留)、routes.py、planning.py、ledger.py、settings.py、dashboard.py、adapters.py、mcp_config.py(GET /api/mcp/config、TOOL_FORMATS、render_config;M3b,design D36)
│   │   └── mcp/                 # MCP server(M3b,design D6、D34、D35、D37)
│   │       ├── __init__.py      # 匯出 McpEndpoint、McpRuntime、SpillwayServer、mcp_catalog
│   │       ├── auth.py          # McpEndpoint(/mcp 的 ASGI wrapper:depth → Bearer → 停機 → 群組;session 綁群組)
│   │       ├── server.py        # SpillwayServer、McpRuntime、install_shutdown_hook、close_standalone_streams
│   │       ├── tools.py         # ask、list_routes、quota_status 的定義與實作
│   │       ├── events.py        # McpEvents(list_changed)
│   │       ├── catalog.py       # mcp_catalog(UI-8 資料來源)、HTTP_ERRORS、TOOL_ERRORS、SDK_TRANSPORT_ERRORS
│   │       ├── sdk_compat.py    # SDK 私有屬性的唯一存取點(get_stream_open、iter_transports)
│   │       └── bridge.py        # spillway mcp-stdio 的 stdio ↔ Streamable HTTP 轉送與錯誤對應層
│   └── templates/
│       └── mcp/                 # 設定輸出模板(M3b;`<tool>.<kind>.j2`,由 TOOL_FORMATS 對應,可由多個 variant 共用;package data)
│           ├── claude-code.project.j2   # .mcp.json(type: http)
│           ├── claude-code.command.j2   # claude mcp add 命令
│           ├── claude-code.stdio.j2     # stdio:.mcp.json(type: stdio)
│           ├── codex.toml.j2            # ~/.codex/config.toml
│           ├── codex.command.j2         # codex mcp add 命令
│           ├── codex.stdio.j2           # stdio:config.toml
│           ├── cursor.http.j2           # ~/.cursor/mcp.json、.cursor/mcp.json
│           ├── cursor.stdio.j2          # stdio:~/.cursor/mcp.json
│           ├── gemini.http.j2           # ~/.gemini/settings.json(httpUrl)
│           └── gemini.stdio.j2          # stdio:~/.gemini/settings.json(gemini variants[1] 與 stdio 共用)
└── tests/
    ├── __init__.py
    ├── conftest.py              # anyio_backend(全 tests/ 唯一定義)、載入 tests/fakes/plugin.py;真實 claude/codex 護欄(session 起始清理 PATH,M3a)
    ├── helpers/                 # 測試輔助套件(M3b 由 tests/helpers.py 改為套件,內容不變)
    │   ├── __init__.py          # 假金鑰 fixture、成員建構器、MemoryKeyring、dead_base
    │   └── live_gateway.py      # live_gateway harness:背景 thread 的 uvicorn.Server,127.0.0.1 隨機埠、create_app(port=…) 同埠(M3b)
    ├── fakes/
    │   ├── __init__.py
    │   ├── api_upstream/        # __init__.py、__main__.py、server.py:假 API 上游(OpenAI 相容與 Anthropic 協定;三個內建 API 模組共用,M2)
    │   ├── cli_upstream/        # __init__.py、fake.py、harness.py:假 claude、codex 執行檔(以 sys.executable 產生的 wrapper)、控制檔與記錄檔協定(M3a,design D5)
    │   ├── claude_cli/          # __init__.py:claude_cli 的 call_status_harness(供 tests/contract/test_call_status.py,M3a)
    │   ├── codex_cli/           # __init__.py:codex_cli 的 call_status_harness(M3a)
    │   ├── plugin.py            # `fake_api` pytest fixture(由 tests/conftest.py 以 pytest_plugins 載入)
    │   ├── test_api_upstream.py # 假 API 上游自身的測試
    │   └── test_cli_upstream.py # 假 CLI 與護欄自身的測試(M3a)
    ├── contract/                # __init__.py、fakemods.py(在暫存目錄寫出假 adapter 模組)、test_adapter_contract.py、test_call_status.py、test_exclude_reasons.py、test_layering.py(分層與 SQL 位置)、test_public_imports.py、test_public_imports_admin.py(含 /api/mcp/config)、test_public_imports_routing.py;無 conftest
    ├── unit/                    # conftest.py、test_clock.py、test_credentials.py、test_domain_constants.py、test_effort.py、test_estimate.py、test_estimate_review_w3.py、test_helpers.py、test_ledger.py、test_quota.py、test_quota_rows.py(M3b)、test_redact.py、test_settings.py;只有 unit/conftest.py
    │   ├── adapters/            # __init__.py、cases.py、test_api_calls.py、test_api_manifests.py、test_claude_cli.py、test_codex_cli.py、test_litellm_module.py、test_review_w3_fixes.py
    │   ├── adapters_base/       # __init__.py、test_api_adapter_seams.py、test_classify.py、test_cli_common.py(M3b)、test_cli_cwd_adapters.py(M3b)、test_cli_prompt.py、test_cli_runner.py、test_effort_and_protocol.py、test_litellm_call.py、test_litellm_logger.py、test_manifest.py、test_probe.py、test_registry.py、test_review_w3_fixes.py
    │   ├── api/                 # test_drain.py(M3b;無 __init__.py)
    │   ├── routing/             # __init__.py、helpers.py、router_helpers.py、test_estimate_cli_regression.py、test_filter.py、test_planner_selector.py、test_router.py、test_router_cli_slots.py、test_target_resolver.py
    │   └── store/               # test_db_conn.py、test_migration_nonempty.py、test_migration_v1_v2.py、test_repo.py、test_repo_cooldowns.py、test_repo_queries.py、test_schema.py(無 __init__.py)
    ├── integration/             # __init__.py、conftest.py、test_app_admin_mount.py、test_errors_openai.py、test_graceful_shutdown.py、test_host_origin.py(M3b)、test_init_token.py、test_keyring_credential.py、test_mcp_stdio.py(M3b)、test_openai_api.py、test_request_conn.py、test_retention_schedule.py、test_review_final_u10.py、test_review_w6_openai.py、test_router_cli_concurrency.py、test_serve.py
    │   ├── admin/               # 管理 API 整合測試:__init__.py、conftest.py(make_app 呼叫 create_app())、helpers.py、test_admin_adapters.py、test_admin_app_state.py、test_admin_dashboard.py、test_admin_delete_cascade.py、test_admin_deployments.py、test_admin_groups.py、test_admin_health_invalidate.py、test_admin_ledger.py、test_admin_mcp_config.py(M3b)、test_admin_route_class.py、test_admin_routes.py、test_admin_secret_keys.py、test_admin_settings.py、test_admin_token_rotate.py、test_errors_admin.py、test_review_w6_admin.py
    │   └── mcp/                 # MCP 整合測試(M3b):__init__.py、conftest.py(live_gateway fixture;註明 ASGITransport 只用於 HTTP 層與 POST 類工具呼叫)、test_live_gateway.py、test_mcp_auth.py、test_mcp_catalog.py、test_mcp_groups.py、test_mcp_list_changed.py、test_mcp_quota_queries.py、test_mcp_shutdown.py、test_mcp_tools.py、test_sdk_compat.py(絆線)
    ├── features/                # behave:__init__.py、environment.py(含真實 claude/codex 護欄)、m2_adapters.feature、m2_context.feature、m2_estimate.feature、m2_quota.feature、m2_routing.feature、m2c_cleanup.feature、m3a_cli.feature、m3b_mcp.feature(M3b)
    │   ├── steps/               # adapters_steps.py、cleanup_ac4_steps.py、cleanup_admin_steps.py、cleanup_health_steps.py、common_steps.py、context_steps.py、estimate_steps.py、m3a_steps.py、m3b_steps.py(M3b)、quota_steps.py、routing_steps.py
    │   └── support/             # __init__.py、gateway.py(行程內驅動 app,與 open_spillway 同方式)、mcp_proc.py(M3b:MCP 用戶端與子行程 serve、mcp-stdio 的共用驅動,m3b 步驟與 test_m3b_operation.py 共用)
    └── acceptance/              # __init__.py、conftest.py(沿用 integration 的 open_spillway、test_key_env、artifacts_dir)、perf.py、procs.py、results.py、seed.py(含 2 個 cli 成員)、test_ac07_dry_run_p95.py、test_m3a_operation.py、test_m3b_operation.py(M3b 操作驗收:認證/工具、list_changed、stdio、停機)、test_nfr1_dry_run_p95.py(M3b 補 task route 變體)、test_nfr2_dashboard_p95.py、test_nfr3_bind.py、test_nfr4_prescan.py、test_nfr5_payload_expiry.py、test_operation.py
```

M4 預計新增(尚未建立):`src/spillway/configio.py`(YAML 匯入匯出)、`src/spillway/templates/web/*.html`(UI-1–UI-8)、`src/spillway/static/`(少量 JS 與 CSS)。樹狀中的 `src/` 與 `tests/` 與 `git ls-tree -r HEAD --name-only` 一致(adapter 模組資料夾內的四個檔與 `tests/fakes`、`tests/contract`、`tests/unit`、`tests/integration`、`tests/features`、`tests/acceptance` 的檔名以註解列出)。

## 資料目錄(repo 外;design D8)

```
$SPILLWAY_HOME/                  # 未設時為 platformdirs.user_data_dir("spillway")
├── spillway.db                  # SQLite(schema v2);WAL 模式另有 spillway.db-wal、spillway.db-shm
├── spillway.lock                # 資料目錄鎖(serve 持有到行程結束;init 執行期間持有;design D28)
├── spillway.db.v1.bak           # v1→v2 升級前的備份(單一檔案);已存在時另存 spillway.db.v1.bak.<UTC 時間戳記>
└── work/
    ├── claude/                  # claude_cli 未設定 params.cwd 時的工作目錄(spillway init 建立)
    └── codex/                   # codex_cli 未設定 params.cwd 時的工作目錄(spillway init 建立)
```

## 各目錄職責

| 路徑 | 職責 | 對應邏輯模組 |
|------|------|--------------|
| `src/spillway/api/` | HTTP 介面;`admin/` 為管理 API 套件(含 MCP 設定輸出 `mcp_config.py`);`host_origin.py` 全端點 Host/Origin;`drain.py` 停機期滿 helper | `api.openai`、`api.admin`、`api.deps`、`api.host_origin`、`api.drain` |
| `src/spillway/api/mcp/` | MCP server、stdio 橋接(`bridge.py`)、UI-8 資料來源(`catalog.py`)、SDK 私有屬性存取(`sdk_compat.py`) | `api.mcp`、`api.mcp.bridge` |
| `src/spillway/templates/mcp/` | MCP 設定輸出模板(`<tool>.<kind>.j2`,由 `TOOL_FORMATS` 對應,可跨 variant 共用) | `api.admin.mcp_config` |
| `src/spillway/templates/web`、`static/`(M4) | Web UI | `web` |
| `src/spillway/routing/` | 路由管線 | `routing` |
| `src/spillway/adapters/base/` | adapter 共用契約、自動發現、訊號歸類與 effort 轉換、CLI 子行程執行器與共用輔助(`cli_runner.py`、`cli_prompt.py`、`cli_common.py`) | `adapters.base` |
| `src/spillway/adapters/<module>/` | 一種 AI 一個模組 | `adapters.<module>` |
| `tests/contract/` | adapter 合約測試、分層 import 檢查 | — |
| `src/spillway/store/` | SQLite | `store` |
| `tests/fakes/` | 假上游(Guideline Q1);API 共用 `api_upstream/`,CLI 共用 `cli_upstream/`(各 CLI 模組的 harness 在 `<module>/`) | — |
| `tests/features/` | behave 行為情境與步驟 | — |
| `tests/helpers/` | 測試輔助套件;`live_gateway.py` 為需要 GET SSE、`list_changed`、stdio 或停機的測試用的背景 uvicorn harness | — |
| `artifacts/` | 驗收與量測產物,不進版控;每筆 CHG 一個子目錄(`m2/`、`m2c/`、`m3a/`、`m3b/`) | — |
| `tests/acceptance/` | 驗收項,檔名帶 AC 編號 | — |

## 命名與放置規則

- 套件採 `src/` layout;模組名小寫底線。
- 新增一種 AI:只新增 `adapters/<module>/` 子套件(含 `MANIFEST`、`create()`、`models.yaml`、`README.md`)與 `tests/fakes/<module>/`,不修改任何既有檔案;須通過 `tests/contract/`(design D11)。模組名小寫底線,`provider` 值用連字號(例:模組 `gemini_cli` 處理 provider `gemini-cli`)。
- 新增或下架某個 AI 的模型:只改該模組的 `models.yaml`(design D12)。
- 新增 MCP 設定輸出的工具:只加 `templates/mcp/<tool>.<kind>.j2`(可共用既有模板)與 `api/admin/mcp_config.py` 的 `TOOL_FORMATS` 一行(spec 8.2.3、design D36)。
- 驗收測試檔名 `test_ac<編號>_<簡述>.py`(例:`test_ac07_dry_run_p95.py`),NFR 量測檔名 `test_nfr<n>_<簡述>.py`,讓 ACC 可直接指到。以行為情境驗證的 AC(M2:AC-3、4、5、13、14、15、18、19)放在 `tests/features/m2_*.feature`,情境標題帶 AC 編號。
- 不在 repo 存任何憑證、資料庫檔或 payload;資料目錄在 repo 外(design D8)。測試一律以 `tmp_path` 為 `SPILLWAY_HOME`,不可讀寫擁有者的資料目錄。

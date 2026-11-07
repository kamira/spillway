# 目錄結構 — Spillway

- 專案:spillway · 分支:main · 版本:v1.7(2026-11-07,CHG-20261107-01:`requirements.lock`、`requirements-dev.lock`、`build-constraints.in`、`build-constraints.lock`、`scripts/check_venv_integrity.py`、`tests/helpers/lockfile.py`、`tests/contract/test_dependency_lock.py`、`tests/unit/test_venv_matches_lock.py`、`tests/unit/test_check_venv_integrity.py`、`sdlc_docs/knowledge/rust-evaluation.md`;升版流程正本見 README「升級相依」) · 前版 v1.6(2026-11-03,CHG-20261103-01:`src/spillway/web/`(`__init__.py` 與 `static/` 的 HTML、CSS、JS,取代原「M4b 預計新增」)、`api/http_headers.py`、`routing/static_plan.py`、`api/admin/config_http.py`(原名 `config_io`)、`tests/ui/`(含 `__init__.py`)、`tests/helpers/ui.py` 與 `ui_*.py`、新增的單元與整合測試、`m4b_ui.feature`、T9 新增的 `m4b_steps.py`、`test_m4b_operation.py` 與 `artifacts/m4b/`;樹狀與 `git ls-tree` 對齊;使用者於 CHG 確認閘確認,2026-11-03) · 前版 v1.5 已確認(2026-11-02,CHG-20261102-02:`configio.py`(由「M4 預計新增」移入樹狀)、`api/admin/config_file.py`、`api/admin/config_io.py`、資料目錄 `backups/`、`tests/unit/data/config_golden.yaml` 與 `tests/helpers/config_golden.py`、新增的單元、合約、整合與操作驗收測試、`m4a_config.feature` 與 `m4a_steps.py`、`artifacts/m4a/`;樹狀與 `git ls-tree` 對齊;使用者於 CHG 確認閘確認,2026-11-02) · 前版 v1.4 已確認(2026-11-02,CHG-20261102-01:`api/mcp/`(含 `sdk_compat.py`、`bridge.py`)、`api/host_origin.py`、`api/drain.py`、`api/admin/mcp_config.py`、`templates/mcp/`、`quota.py` 的 `member_quota_rows`、`adapters/base/cli_common.py` 與 `adapters/base` 全部檔名、`tests/helpers/` 套件與 `live_gateway.py`、`tests/integration/mcp/`、`m3b_mcp.feature` 與 `m3b_steps.py`、`artifacts/m3b/`;樹狀與 `git ls-tree` 對齊;使用者於 CHG 確認閘確認,2026-11-02) · 前版 v1.3 已確認(2026-11-01,CHG-20261101-03:`adapters/base/cli_runner.py`、`cli_prompt.py`、`adapters/claude_cli/`、`adapters/codex_cli/`、`tests/fakes/cli_upstream/`、`tests/fakes/claude_cli/`、`tests/fakes/codex_cli/`、`m3a_cli.feature` 與 `m3a_steps.py`、`artifacts/m3a/`、資料目錄內容;使用者於 CHG 確認閘確認,2026-11-01) · 前前版 v1.2 已確認(2026-11-01,CHG-20261101-02:`api/admin/` 套件、`api/deps.py`、`routing/outcome.py`、`adapters/base/api_adapter.py`、`tests/helpers.py`、`m2c_cleanup.feature` 與 `cleanup_*_steps.py`、`artifacts/m2c/`;移除 tests/contract、tests/unit/adapters、tests/unit/adapters_base、tests/unit/routing 的 conftest;使用者於 CHG 確認閘確認,2026-11-01) · v1.1 已確認(2026-10-30,CHG-20261030-02) · v1.0 已確認(2026-10-29,使用者) · 日期:2026-11-03(UTC+0)
- 依據:sdlc_docs/structure/logical.md、design.md

## 樹狀

```
spillway/
├── AGENTS.md                    # AI 入口
├── README.md
├── .gitignore                   # .venv/、__pycache__/、*.egg-info/、.pytest_cache/、artifacts/
├── pyproject.toml               # 套件、相依(M3b 明列 mcp>=1.30,<2、jinja2>=3、sse-starlette>=3.5.0;M4b dev 加 playwright==1.64.1)、console script `spillway`、package data(含 templates/mcp/*.j2;M4b 加 web/static/** 的 *.html、*.css、*.js)
├── requirements.lock            # 執行期相依的雜湊 lock(Python ≥3.11、三平台;CHG-20261107-01 決策 3;`uv pip sync --require-hashes --no-build`)
├── requirements-dev.lock        # 開發與測試相依的雜湊 lock(Python ≥3.12;決策 3、8)
├── build-constraints.in         # 建置 `-e .` 用的 setuptools 版本(決策 9)
├── build-constraints.lock       # 上者的雜湊 lock(`--build-constraint`)
├── scripts/
│   └── check_venv_integrity.py  # venv 入侵檢查(`python3 -I`;.pth、sitecustomize、RECORD 雜湊;決策 6 ⑤)
├── docs/                        # 企劃書、規格書、UI 設計稿(需求來源,不放帳本)
├── sdlc_docs/                   # 治理帳本(knowledge/ 另有 rust-evaluation.md:Rust 評估報告,CHG-20261107-01)
├── artifacts/                   # 驗收與量測產物(不進版控,M2 起)
│   ├── m2/                      # CHG-20261030-02 的驗收產物
│   ├── m2c/                     # CHG-20261101-02 的驗收產物(integration.xml、contract.xml、http-log.jsonl)
│   ├── m3a/                     # CHG-20261101-03 的驗收產物(integration.xml、contract.xml、http-log.jsonl:只含 method、path、status、elapsed_ms)
│   ├── m3b/                     # CHG-20261102-01 的驗收產物(integration.xml、cli.xml、contract.xml、http-log.jsonl〔四鍵,含 MCP 請求〕、operation-results.json)
│   ├── m4a/                     # CHG-20261102-02 的驗收產物(integration.xml、cli.xml、contract.xml 由 Interaction spec 指令產生,不進版控、不一定存在於工作樹;http-log.jsonl〔四鍵〕、operation-results.json 由操作驗收產生)
│   └── m4b/                     # CHG-20261103-01 的驗收產物(操作驗收時產生,不進版控):ui.xml、integration.xml、contract.xml(Interaction spec 指令)、operation-results.json、每個畫面一張 1280×800 截圖(不含 token 對話框)、AC-8 計時
├── src/spillway/
│   ├── __init__.py
│   ├── __main__.py / cli.py     # spillway serve(M2;M3a 加 _preflight 與 graceful shutdown;M3b 停用 sse-starlette auto-drain、shutdown_hooks、drain_expired、Ctrl-C 結束碼 130)、init [--rotate](M3a)、mcp-stdio --group <g> [--port](M3b)
│   ├── app.py                   # FastAPI 組裝 create_app(issue_token=False, port=8787)、最外層 HostOriginMiddleware、其內 SecurityHeadersMiddleware(M4b)、/mcp 掛載、mount_web(/ui 與 / 轉址,M4b)、lifespan(背景清理工作、CliRunner 注入、token、MCP session manager 持有 task、D31 關閉順序、terminate_all 後備)
│   ├── settings.py              # 資料目錄、埠號、綁定常數;存取 token 產生與雜湊讀寫(M3a,經 store.repo);MCP_ALL_GROUP、EXIT_SIGINT、mcp_url(M3b 終審);TOKEN_ENV、DEPTH_ENV、DEPTH_HEADER(M4a,design D35);package_version()(M4b,版本字串單一定義)
│   ├── clock.py                 # Clock、Rng;utc_strftime、utc_stamp(M4b,檔名時間戳記)
│   ├── configio.py              # 設定檔格式與匯入匯出(M4a,應用層;design D38):dump、FORMAT_VERSION、_Dumper、load_yaml(嚴格 loader)、ConfigPlan、apply、ImportResult、ImportConflict、ConfigSyntaxError、is_privileged_change(M4b);不可 import spillway.api*
│   ├── domain.py                # 跨層值物件 Deployment、Budget、ChatRequest、CallResult(adapters.base 轉匯出);共用常數 KINDS、CLOUD_KINDS、CALL_STATUSES、LEDGER_STATUSES、COOLING_STATUSES、STATUS_SKIPPED_PRECHECK
│   ├── store/
│   │   ├── __init__.py
│   │   ├── db.py                # 連線、PRAGMA、migration(user_version;v1→v2 的外鍵預檢、備份、交易內升級)、資料目錄鎖 spillway.lock、ConnSource / use_conn(不關閉連線)
│   │   ├── schema.sql           # schema v1 基線(新庫先套 v1);v2 由 db.py 的 migration `_SCHEMA_V2` 套用,migration 後結果與 spec 2.3 一致
│   │   └── repo.py              # 各表存取(全專案的 SQL 只在 store/;冷卻函式讀寫 cooldowns);M4a:iter_ledger、payload_stats、count_config、「# config 匯出讀取」(read_snapshot、list_all_route_targets)、「# config 匯入寫入」(delete_settings、deployment_ids_by_provider);M4b:read_tx(讀交易)、帳本篩選 min_depth
│   ├── credentials.py
│   ├── redact.py                # Redactor、共用實例 REDACTOR
│   ├── routing/
│   │   ├── __init__.py          # 公開匯出(Router、OrderPlanner、members_of、RouteOutcome 等)
│   │   ├── target.py            # route 目標(group / tags)→ 成員集合(members_of);normalize_selector(M3b 終審)
│   │   ├── resolver.py
│   │   ├── filter.py            # CandidateFilter、HealthCache(invalidate)
│   │   ├── planner.py
│   │   ├── selector.py
│   │   ├── outcome.py           # RouteOutcome、AttemptInfo、Plan、PlanSlot、PlanEntry、PlanExcluded
│   │   ├── static_plan.py       # static_plan(靜態順序計畫;effective-order 與 MCP list_routes 共用,M4b,design D43)
│   │   └── router.py            # Router(invalidate_health)
│   ├── quota.py                 # QuotaService;member_quota_rows(M4b 加 daily_spend_7d)、is_cooling_only(M3b:/api/dashboard 與 MCP quota_status 共用;上游訊號判定 `classify` 在 adapters/base/)
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
│   │   ├── http_headers.py      # SecurityHeadersMiddleware、headers_for_path、CSP(純 ASGI 安全標頭;M4b,design D42)
│   │   ├── drain.py             # run_until_drain、Drained、ShutdownHooks、CANCELLED_DRAIN_TIMEOUT(M3b,design D31)
│   │   ├── admin/               # 管理 API 套件:__init__.py(組 router)、common.py(含 tools_changed)、models.py、secrets.py、deployments.py、groups.py(群組名稱規則 group_name_problem 定義於 models.py;M4a,design D22)、routes.py、planning.py、ledger.py(含 GET /api/ledger/export.csv;M4a,design D39)、settings.py(含 GET /api/settings;M4a,design D40)、dashboard.py、adapters.py、mcp_config.py(GET /api/mcp/config、TOOL_FORMATS、render_config;M3b,design D36)、config_file.py(設定檔模型 ConfigFileIn 與 parse;M4a,design D38)、config_http.py(GET /api/config/export、POST /api/config/import;M4a,design D38;原名 config_io,M4b 更名);M4b 新增 POST /api/groups/preview(groups.py)、GET /api/routes/{name}/effective-order(routes.py)、GET /api/mcp/catalog(mcp_config.py),422 一律經 common.invalid(design D43)
│   │   └── mcp/                 # MCP server(M3b,design D6、D34、D35、D37)
│   │       ├── __init__.py      # 匯出 McpEndpoint、McpRuntime、SpillwayServer、mcp_catalog
│   │       ├── auth.py          # McpEndpoint(/mcp 的 ASGI wrapper:depth → Bearer → 停機 → 群組;session 綁群組)
│   │       ├── server.py        # SpillwayServer、McpRuntime、install_shutdown_hook、close_standalone_streams
│   │       ├── tools.py         # ask、list_routes、quota_status 的定義與實作
│   │       ├── events.py        # McpEvents(list_changed)
│   │       ├── catalog.py       # mcp_catalog(UI-8 資料來源)、HTTP_ERRORS、TOOL_ERRORS、SDK_TRANSPORT_ERRORS
│   │       ├── sdk_compat.py    # SDK 私有屬性的唯一存取點(get_stream_open、iter_transports)
│   │       ├── sdk_logging.py   # 丟棄 SDK 經 root logger 寫出的紀錄(避免 prompt 片段;M3b 終審)
│   │       └── bridge.py        # spillway mcp-stdio 的 stdio ↔ Streamable HTTP 轉送與錯誤對應層
│   ├── templates/
│   │   └── mcp/                 # 設定輸出模板(M3b;`<tool>.<kind>.j2`,由 TOOL_FORMATS 對應,可由多個 variant 共用;package data)
│   │       ├── claude-code.project.j2   # .mcp.json(type: http)
│   │       ├── claude-code.command.j2   # claude mcp add 命令
│   │       ├── claude-code.stdio.j2     # stdio:.mcp.json(type: stdio)
│   │       ├── codex.toml.j2            # ~/.codex/config.toml
│   │       ├── codex.command.j2         # codex mcp add 命令
│   │       ├── codex.stdio.j2           # stdio:config.toml
│   │       ├── cursor.http.j2           # ~/.cursor/mcp.json、.cursor/mcp.json
│   │       ├── cursor.stdio.j2          # stdio:~/.cursor/mcp.json
│   │       ├── gemini.http.j2           # ~/.gemini/settings.json(httpUrl)
│   │       └── gemini.stdio.j2          # stdio:~/.gemini/settings.json(gemini variants[1] 與 stdio 共用)
│   └── web/                     # Web UI(M4b,design D41):Python 端只 import 標準庫與 starlette
│       ├── __init__.py          # mount_web(app)、WEB_ROOT、ALLOWED_SUFFIXES、is_allowed_path、StaticFilter(純 ASGI 檔案過濾)、/ 的 302 轉址
│       └── static/              # package data;每頁只以 <script type="module" src="assets/js/<page>.js"> 載入
│           ├── index.html       # UI-1 總覽(/ui/)
│           ├── members.html     # UI-2 成員(#<id> 開啟該成員)
│           ├── groups.html      # UI-3 群組與 Tag
│           ├── routes.html      # UI-4 Route 清單
│           ├── route.html       # UI-4 編輯頁(?name=<route>)
│           ├── mcp.html         # UI-5 MCP 設定
│           ├── ledger.html      # UI-6 帳本
│           ├── settings.html    # UI-7 設定(#token)
│           ├── wizard.html      # 首次設定精靈
│           ├── api-reference.html # UI-8 API 參考
│           └── assets/          # app.css(共用元件)、api-reference.css、groups.css、ledger.css、mcp.css、member-form.css、members.css、overview.css、routes.css、settings.css
│               └── js/          # 共用:api.js、dom.js、ui.js(含 T9 新增的 copyToClipboard)、strings.js、member-form.js、import-dialog.js;各頁:overview.js、members.js、groups.js、routes.js、route-editor.js、mcp.js、ledger.js、settings.js、wizard.js、api-reference.js
└── tests/
    ├── __init__.py
    ├── conftest.py              # anyio_backend(全 tests/ 唯一定義)、載入 tests/fakes/plugin.py;真實 claude/codex 護欄(session 起始清理 PATH,M3a)
    ├── helpers/                 # 測試輔助套件(M3b 由 tests/helpers.py 改為套件,內容不變)
    │   ├── __init__.py          # 假金鑰 fixture、成員建構器、MemoryKeyring、dead_base、redact_tokens(M4a)
    │   ├── config_golden.py     # 黃金檔的種入資料與全表快照(M4a;T3、T4、T6 共用)
    │   ├── lockfile.py          # 雜湊 lock 解析(`parse_lock`、`canon`、`BLOCKED_LITELLM`;CHG-20261107-01)
    │   ├── live_gateway.py      # live_gateway harness:背景 thread 的 uvicorn.Server,127.0.0.1 隨機埠、create_app(port=…) 同埠(M3b)
    │   ├── ui.py               # 畫面測試共用 helper(M4b):PageGuard(console、pageerror、非本機請求、dialog)、open_context、goto、wait_loaded、query_of、單一 DB adapter 層(db_snapshot、db_execute)、種資料(api、member_body、add_member、add_route、seed_group);T9 新增的 drag、confirm_dialog、CLIP
    │   ├── ui_ledger.py         # UI-6 helper(M4b)
    │   ├── ui_mcp.py            # UI-5、UI-8 helper(M4b)
    │   ├── ui_members.py        # UI-2、UI-3 helper(M4b)
    │   ├── ui_overview.py       # UI-1 helper(M4b)
    │   ├── ui_routes.py         # UI-4 helper(拖曳等,M4b)
    │   └── ui_settings.py       # UI-7 與精靈 helper(M4b)
    ├── fakes/
    │   ├── __init__.py
    │   ├── api_upstream/        # __init__.py、__main__.py、server.py:假 API 上游(OpenAI 相容與 Anthropic 協定;三個內建 API 模組共用,M2)
    │   ├── cli_upstream/        # __init__.py、fake.py、harness.py:假 claude、codex 執行檔(以 sys.executable 產生的 wrapper)、控制檔與記錄檔協定(M3a,design D5)
    │   ├── claude_cli/          # __init__.py:claude_cli 的 call_status_harness(供 tests/contract/test_call_status.py,M3a)
    │   ├── codex_cli/           # __init__.py:codex_cli 的 call_status_harness(M3a)
    │   ├── plugin.py            # `fake_api` pytest fixture(由 tests/conftest.py 以 pytest_plugins 載入)
    │   ├── test_api_upstream.py # 假 API 上游自身的測試
    │   └── test_cli_upstream.py # 假 CLI 與護欄自身的測試(M3a)
    ├── contract/                # __init__.py、fakemods.py(在暫存目錄寫出假 adapter 模組)、test_dependency_lock.py(雜湊 lock 檔契約,CHG-20261107-01)、test_adapter_contract.py、test_call_status.py、test_env_names.py(M4a:環境變數名與 adapters 字面值、模板、catalog 相等)、test_exclude_reasons.py、test_import_admin_parity.py(M4a:匯入與管理 API 規則一致)、test_layering.py(分層與 SQL 位置)、test_public_imports.py、test_public_imports_admin.py(含 /api/mcp/config、M4a 新端點)、test_public_imports_routing.py;無 conftest
    ├── unit/                    # conftest.py、test_check_venv_integrity.py(CHG-20261107-01)、test_venv_matches_lock.py(漂移偵測,CHG-20261107-01)、test_clock.py、test_configio_dump.py(M4a)、test_configio_parse.py(M4a)、test_credentials.py、test_domain_constants.py、test_effort.py、test_estimate.py、test_estimate_review_w3.py、test_helpers.py、test_ledger.py、test_quota.py、test_quota_rows.py(M3b)、test_redact.py、test_settings.py、test_web_assets.py(M4b:禁用字樣靜態掃描、無 inline script/style、導覽一致、web 的 import 邊界、package-data glob);只有 unit/conftest.py
    │   ├── adapters/            # __init__.py、cases.py、test_api_calls.py、test_api_manifests.py、test_claude_cli.py、test_codex_cli.py、test_litellm_module.py、test_review_w3_fixes.py
    │   ├── adapters_base/       # __init__.py、test_api_adapter_seams.py、test_classify.py、test_cli_common.py(M3b)、test_cli_cwd_adapters.py(M3b)、test_cli_prompt.py、test_cli_runner.py、test_effort_and_protocol.py、test_litellm_call.py、test_litellm_logger.py、test_manifest.py、test_probe.py、test_registry.py、test_review_w3_fixes.py
    │   ├── api/                 # test_admin_invalid.py(M4b)、test_drain.py(M3b;無 __init__.py)
    │   ├── data/                # config_golden.yaml(設定匯出黃金檔,M4a;每欄都有非預設值)
    │   ├── routing/             # __init__.py、helpers.py、router_helpers.py、test_estimate_cli_regression.py、test_filter.py、test_planner_selector.py、test_router.py、test_router_cli_slots.py、test_target_resolver.py
    │   └── store/               # test_db_conn.py、test_migration_nonempty.py、test_migration_v1_v2.py、test_repo.py、test_read_tx.py(M4b)、test_repo_cooldowns.py、test_repo_queries.py、test_schema.py(無 __init__.py)
    ├── integration/             # __init__.py、conftest.py、test_app_admin_mount.py、test_errors_openai.py、test_graceful_shutdown.py、test_host_origin.py(M3b)、test_init_token.py、test_keyring_credential.py、test_mcp_stdio.py(M3b)、test_openai_api.py、test_request_conn.py、test_retention_schedule.py、test_review_final_u10.py、test_review_w6_openai.py、test_router_cli_concurrency.py、test_serve.py、test_web_static.py(M4b:mount_web 與 SecurityHeadersMiddleware 的路徑、標頭、方法、檔案過濾)
    │   ├── admin/               # 管理 API 整合測試:__init__.py、conftest.py(make_app 呼叫 create_app())、helpers.py、test_admin_adapters.py、test_admin_app_state.py、test_admin_config_io.py(M4a)、test_admin_dashboard.py、test_admin_delete_cascade.py、test_admin_deployments.py、test_admin_groups.py、test_admin_health_invalidate.py、test_admin_ledger.py、test_admin_ledger_csv.py(M4a)、test_admin_mcp_catalog.py(M4b)、test_admin_mcp_config.py(M3b)、test_admin_route_class.py、test_admin_routes.py、test_admin_secret_keys.py、test_admin_settings.py、test_admin_settings_overview.py(M4a)、test_admin_token_rotate.py、test_errors_admin.py、test_review_w6_admin.py
    │   └── mcp/                 # MCP 整合測試(M3b):__init__.py、conftest.py(live_gateway fixture;註明 ASGITransport 只用於 HTTP 層與 POST 類工具呼叫)、test_live_gateway.py、test_mcp_auth.py、test_mcp_catalog.py、test_mcp_groups.py、test_mcp_list_changed.py、test_mcp_log_leak.py、test_mcp_quota_queries.py、test_mcp_shutdown.py、test_mcp_tools.py、test_mcp_version.py(M4b:serverInfo.version)、test_sdk_compat.py(絆線)
    ├── ui/                      # 畫面測試(M4b,pytest + Playwright;design D41):__init__.py(避免與 tests/unit 同名衝突)、conftest.py(make_ui_gateway、fake_api、假 CLI、module 範圍的 playwright/browser、new_page、expect_http_errors marker、事件迴圈 teardown 斷言)、test_shell.py(10 頁骨架、導覽、api.js/dom.js、對話框)、test_overview.py、test_members.py、test_groups.py、test_routes.py、test_mcp.py、test_api_reference.py、test_ledger.py、test_settings.py、test_wizard.py
    ├── features/                # behave:__init__.py、environment.py(含真實 claude/codex 護欄)、m2_adapters.feature、m2_context.feature、m2_estimate.feature、m2_quota.feature、m2_routing.feature、m2c_cleanup.feature、m3a_cli.feature、m3b_mcp.feature(M3b)、m4a_config.feature(M4a)、m4b_ui.feature(M4b,10 個情境);environment.py 由 T9 新增只限 m4b_ui 的 Playwright before_feature/after_feature
    │   ├── steps/               # adapters_steps.py、cleanup_ac4_steps.py、cleanup_admin_steps.py、cleanup_health_steps.py、common_steps.py、context_steps.py、estimate_steps.py、m3a_steps.py、m3b_steps.py(M3b)、m4a_steps.py(M4a)、m4b_steps.py(M4b,T9 新增;只匯入 tests/helpers/ui.py 與 ui_*.py)、quota_steps.py、routing_steps.py
    │   └── support/             # __init__.py、gateway.py(行程內驅動 app,與 open_spillway 同方式)、mcp_proc.py(M3b:MCP 用戶端與子行程 serve、mcp-stdio 的共用驅動,m3b 步驟與 test_m3b_operation.py 共用)
    └── acceptance/              # __init__.py、conftest.py(沿用 integration 的 open_spillway、test_key_env、artifacts_dir)、perf.py、procs.py、results.py、seed.py(含 2 個 cli 成員)、test_ac07_dry_run_p95.py、test_m3a_operation.py、test_m3b_operation.py(M3b 操作驗收:認證/工具、list_changed、stdio、停機)、test_m4a_operation.py(M4a 操作驗收:匯出、往返、merge、dry_run、replace 備份、CSV、總覽、max_sessions)、test_m4b_operation.py(M4b 操作驗收,T9 新增:精靈計時、8 個畫面、拖曳、匯入確認、AC-10 往返、NFR-4 搜尋)、test_nfr1_dry_run_p95.py(M3b 補 task route 變體)、test_nfr2_dashboard_p95.py、test_nfr3_bind.py、test_nfr4_prescan.py、test_nfr5_payload_expiry.py、test_operation.py
```

樹狀中的 `src/` 與 `tests/` 與 `git ls-tree -r HEAD --name-only` 一致(adapter 模組資料夾內的四個檔、`web/static/assets/` 的 CSS 與 JS,以及 `tests/fakes`、`tests/contract`、`tests/unit`、`tests/integration`、`tests/features`、`tests/acceptance`、`tests/ui` 的檔名以註解列出)。

## 資料目錄(repo 外;design D8)

```
$SPILLWAY_HOME/                  # 未設時為 platformdirs.user_data_dir("spillway")
├── spillway.db                  # SQLite(schema v2);WAL 模式另有 spillway.db-wal、spillway.db-shm
├── spillway.lock                # 資料目錄鎖(serve 持有到行程結束;init 執行期間持有;design D28)
├── spillway.db.v1.bak           # v1→v2 升級前的備份(單一檔案);已存在時另存 spillway.db.v1.bak.<UTC 時間戳記>
├── backups/                     # 設定匯入 replace 前的自動備份(M4a,design D38):config-<UTC yyyymmddThhmmssZ>.yaml(同名時 -2、-3…;不覆寫、不自動清理);目錄新建時 0700、檔案 0600
└── work/
    ├── claude/                  # claude_cli 未設定 params.cwd 時的工作目錄(spillway init 建立)
    └── codex/                   # codex_cli 未設定 params.cwd 時的工作目錄(spillway init 建立)
```

## 各目錄職責

| 路徑 | 職責 | 對應邏輯模組 |
|------|------|--------------|
| `src/spillway/api/` | HTTP 介面;`admin/` 為管理 API 套件(含 MCP 設定輸出 `mcp_config.py`、設定檔模型 `config_file.py` 與匯入匯出端點 `config_http.py`、畫面用的讀取端點);`host_origin.py` 全端點 Host/Origin;`http_headers.py` 安全標頭(M4b);`drain.py` 停機期滿 helper | `api.openai`、`api.admin`、`api.deps`、`api.host_origin`、`api.http_headers`、`api.drain` |
| `src/spillway/api/mcp/` | MCP server、stdio 橋接(`bridge.py`)、UI-8 資料來源(`catalog.py`)、SDK 私有屬性存取(`sdk_compat.py`) | `api.mcp`、`api.mcp.bridge` |
| `src/spillway/templates/mcp/` | MCP 設定輸出模板(`<tool>.<kind>.j2`,由 `TOOL_FORMATS` 對應,可跨 variant 共用) | `api.admin.mcp_config` |
| `src/spillway/web/` | Web UI(M4b):`__init__.py` 掛載與檔案過濾;`static/` 為靜態 HTML、CSS、ES module JS(不經建置;package data) | `web` |
| `src/spillway/configio.py` | 設定檔格式與匯入匯出(應用層;不可 import `spillway.api*`) | `configio` |
| `src/spillway/routing/` | 路由管線 | `routing` |
| `src/spillway/adapters/base/` | adapter 共用契約、自動發現、訊號歸類與 effort 轉換、CLI 子行程執行器與共用輔助(`cli_runner.py`、`cli_prompt.py`、`cli_common.py`) | `adapters.base` |
| `src/spillway/adapters/<module>/` | 一種 AI 一個模組 | `adapters.<module>` |
| `tests/contract/` | adapter 合約測試、分層 import 檢查 | — |
| `src/spillway/store/` | SQLite | `store` |
| `tests/fakes/` | 假上游(Guideline Q1);API 共用 `api_upstream/`,CLI 共用 `cli_upstream/`(各 CLI 模組的 harness 在 `<module>/`) | — |
| `tests/features/` | behave 行為情境與步驟 | — |
| `tests/helpers/` | 測試輔助套件;`live_gateway.py` 為需要 GET SSE、`list_changed`、stdio 或停機的測試用的背景 uvicorn harness;`config_golden.py` 為黃金檔的種入資料與全表快照;`ui.py` 為畫面測試共用 helper(含唯一的 DB adapter 層),`ui_<area>.py` 為各畫面自有的 helper(behave 步驟只匯入、不搬移) | — |
| `tests/ui/` | 畫面測試(pytest + Playwright,Chromium 1248 headless);全量 `pytest -q` 包含本目錄;主 thread 不可使用 asyncio | — |
| `tests/unit/data/` | 單元測試的資料檔(設定匯出黃金檔 `config_golden.yaml`;改動須經修改治理) | — |
| `artifacts/` | 驗收與量測產物,不進版控;每筆 CHG 一個子目錄(`m2/`、`m2c/`、`m3a/`、`m3b/`、`m4a/`、`m4b/`) | — |
| `tests/acceptance/` | 驗收項,檔名帶 AC 編號 | — |

## 命名與放置規則

- 套件採 `src/` layout;模組名小寫底線。
- 新增一種 AI:只新增 `adapters/<module>/` 子套件(含 `MANIFEST`、`create()`、`models.yaml`、`README.md`)與 `tests/fakes/<module>/`,不修改任何既有檔案;須通過 `tests/contract/`(design D11)。模組名小寫底線,`provider` 值用連字號(例:模組 `gemini_cli` 處理 provider `gemini-cli`)。
- 新增或下架某個 AI 的模型:只改該模組的 `models.yaml`(design D12)。
- 新增 MCP 設定輸出的工具:只加 `templates/mcp/<tool>.<kind>.j2`(可共用既有模板)與 `api/admin/mcp_config.py` 的 `TOOL_FORMATS` 一行(spec 8.2.3、design D36)。
- 驗收測試檔名 `test_ac<編號>_<簡述>.py`(例:`test_ac07_dry_run_p95.py`),NFR 量測檔名 `test_nfr<n>_<簡述>.py`,讓 ACC 可直接指到。以行為情境驗證的 AC(M2:AC-3、4、5、13、14、15、18、19)放在 `tests/features/m2_*.feature`,情境標題帶 AC 編號。
- 不在 repo 存任何憑證、資料庫檔或 payload;資料目錄在 repo 外(design D8)。測試一律以 `tmp_path` 為 `SPILLWAY_HOME`,不可讀寫擁有者的資料目錄。

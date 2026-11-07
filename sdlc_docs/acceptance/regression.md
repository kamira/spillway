# 累積迴歸集(regression)

單次驗收只回答「新東西做對了嗎」;本檔累積歷次 ACC 中**可程式化驗證**的條件,回答「舊東西被弄壞了嗎」。

## 使用規則

- **每次中/高風險變更**驗收時,必須跑本檔中「範圍標籤」與該 CHG 影響範圍相交的項目;**弄壞舊條件即為未通過**,即使新條件全過,照一般未達標交回 modification-guide 修正。低風險變更:跑影響範圍明顯碰到的項目即可。
- 每次 ACC 收尾時,把該 ACC 可程式化的條件登記進來;不重複登記,範圍標籤用擴充的。
- 執行指向以反引號包住;doc-integrity lint 會檢查反引號內的檔案路徑仍存在。被刪掉的測試等於靜默作廢的承諾,刪測試時必須同步更新本檔並說明理由。
- 本檔只「指向」測試與指令,不重造測試框架。
- 執行環境:repo 根目錄、.venv 內的 Python;驗收時以 PYTHONPATH=src 執行,並設 LITELLM_LOCAL_MODEL_COST_MAP=True 以免連外網。自動測試只打本機假上游(Guideline Q1),不需要真實金鑰。

## 全套指令(M2 起)

| # | 指令 | 通過標準 | 涵蓋條件 | 來源 ACC | 範圍標籤 |
|---|------|----------|----------|----------|----------|
| R1 | `python -m pytest -q` | exit 0;0 failed(ACC-20261031-01 時為 1089 passed;ACC-20261101-01 時為 1340 passed;ACC-20261101-02 時為 1739 passed;ACC-20261102-01 時為 2194 passed;ACC-20261102-02 時為 2578 passed;ACC-20261103-01 時為 3004 passed;ACC-20261107-01 時為 3045 passed;ACC-20261107-02 時為 3057 passed) | 單元、合約、整合、fakes 與 acceptance 全部;含 AC-19 合約測試、管理 API 錯誤碼(FR-13、FR-25)、`stream: true` 400、retention 每小時排程、AC-7、NFR-1、NFR-2、NFR-3、NFR-5 | ACC-20261031-01 | 全部 |
| R2 | `python -m behave tests/features` | exit 0;全部情境全過、0 skipped(ACC-20261031-01 時 5 features、18 scenarios、155 steps;ACC-20261101-01 時 6 features、22 scenarios、183 steps;ACC-20261101-02 時 7 features、31 scenarios、279 steps;ACC-20261102-01 時 8 features、40 scenarios、395 steps;ACC-20261102-02 時 9 features、50 scenarios、498 steps;ACC-20261103-01 時 10 features、60 scenarios、570 steps) | AC-3、AC-4、AC-5、AC-13、AC-14、AC-15、AC-18、AC-19、4.2.5(api 版)、4.2.6(api 版) | ACC-20261031-01 | routing、quota、estimate、ledger、redact、adapters、api |
| R3 | `python -m pytest tests/acceptance -q -rs` | 全數 passed(ACC-20261031-01 時 9、ACC-20261101-01 時 10、ACC-20261101-02 時 12、ACC-20261102-01 時 17、ACC-20261102-02 時 19、ACC-20261103-01 時 20)且**必須 0 skipped**(缺 lsof 時 NFR-3 與操作驗收會被跳過;驗收平台 macOS 有 lsof,出現 skipped 即視為未通過) | AC-7、NFR-1(dry-run P95 < 20 ms)、NFR-2(dashboard P95 < 200 ms)、NFR-3(只聽 127.0.0.1)、NFR-5(payload 過期刪除)、Acceptance operation、NFR-4 預掃描(不計入 M2 閘門) | ACC-20261031-01 | api、cli、routing、ledger、store |

R1 與 R2 合起來就是 CHG-20261030-02 的本機閘(`python -m pytest -q && python -m behave tests/features`)。

## 分項指向

| # | 條件 | 怎麼跑 | 來源 ACC | 範圍標籤 |
|---|------|--------|----------|----------|
| R4 | AC-3:slot 1 皆冷卻時由 slot 2 接手;AC-4:slot 內加權分攤 70%–80%;AC-5:local/<成員> 直呼、帳本 slot NULL;429 冷卻 60 秒後恢復 | `tests/features/m2_routing.feature` | ACC-20261031-01 | routing、quota |
| R5 | AC-13:每次嘗試一列帳本並保存 payload;AC-14:保存期限過後刪除、查詢 410;AC-15:疑似金鑰遮罩並標記 redacted | `tests/features/m2_context.feature` | ACC-20261031-01 | ledger、redact、api |
| R6 | AC-18:dry-run 不呼叫上游、P50/P90 與帳本一致、帳本列數不變 | `tests/features/m2_estimate.feature` | ACC-20261031-01 | estimate、routing |
| R7 | AC-19:只新增模組資料夾即可接入、/api/adapters 列出、可被路由、合約測試通過;cardinality 409、無法 import 的模組不影響啟動 | `tests/features/m2_adapters.feature`、`tests/contract/test_adapter_contract.py`、`tests/contract/test_call_status.py`、`tests/contract/test_layering.py` | ACC-20261031-01 | adapters |
| R8 | 4.2.5(api 版)預估檢查 `skipped_precheck`;4.2.6(api 版)額度用完冷卻到下一重置點 | `tests/features/m2_quota.feature` | ACC-20261031-01 | quota |
| R9 | AC-7、NFR-1:100 成員、10,000 列帳本、1,000 次 dry-run P95 < 20 ms | `tests/acceptance/test_ac07_dry_run_p95.py`、`tests/acceptance/test_nfr1_dry_run_p95.py` | ACC-20261031-01 | routing、效能 |
| R10 | NFR-2:/api/dashboard P95 < 200 ms | `tests/acceptance/test_nfr2_dashboard_p95.py` | ACC-20261031-01 | api、效能 |
| R11 | NFR-3:serve 只聽 127.0.0.1、非本機位址拒絕啟動 | `tests/acceptance/test_nfr3_bind.py` | ACC-20261031-01 | cli、安全 |
| R12 | NFR-5:`usage_ledger` 無內容欄位;payload 過期後由背景排程刪除 | `tests/acceptance/test_nfr5_payload_expiry.py`、`tests/integration/test_retention_schedule.py` | ACC-20261031-01 | ledger、安全 |
| R13 | Acceptance operation(429 → slot 2、local 直呼、遮罩、dry-run 不寫帳本、stream 400、lsof)與遮罩後的 http-log 產物 | `tests/acceptance/test_operation.py` | ACC-20261031-01 | api、routing、安全 |
| R14 | NFR-4 預掃描:資料庫與日誌無金鑰明文(不計入 M2 閘門;M4 閘門才正式驗收) | `tests/acceptance/test_nfr4_prescan.py` | ACC-20261031-01 | 安全 |
| R15 | 管理 API:CRUD、錯誤碼 validation_error、not_found、conflict、provider_single、member_not_in_target、payload_expired;決策 53、54 | `tests/integration/admin/` | ACC-20261031-01 | api(admin) |
| R16 | OpenAI 相容端點:`stream: true` 回 400 `streaming_unsupported`、route_not_found、no_candidate | `tests/integration/test_errors_openai.py`、`tests/integration/test_openai_api.py` | ACC-20261031-01 | api(openai) |
| R20 | 憑證鍵名正規化比對(完全比對集與複合字根,不誤擋 `max_tokens` 等)、整棵 params(含 signals)比對、寫入時 `validate_signals`(null 視同未覆寫)、422 不回顯值 | `tests/integration/admin/test_admin_secret_keys.py`、`tests/features/m2c_cleanup.feature` | ACC-20261101-01 | api(admin)、安全 |
| R21 | 管理 API PUT/DELETE 後健康快取失效(經 route;含對照組);DEBUG 下 LiteLLM 與 openai SDK 不印出 prompt(含無下限對照組);七個 logger 低於 INFO 一律設 INFO | `tests/integration/admin/test_admin_health_invalidate.py`、`tests/unit/adapters_base/test_litellm_logger.py`、`tests/unit/routing/test_filter.py` | ACC-20261101-01 | routing、adapters、安全 |
| R22 | 清理排程提前量 2×poll:抖動 ≤ poll 時間隔 ≤ interval(含 1×poll 對照組);NFR-5 過期起 ≤ 1 小時刪除 | `tests/integration/test_retention_schedule.py`、`tests/acceptance/test_nfr5_payload_expiry.py` | ACC-20261101-01 | ledger |
| R23 | 連線取得:`use_conn` 不關閉、Router 一次請求一條連線、`request_conn` 開關數相等且在事件迴圈執行緒 | `tests/unit/store/test_db_conn.py`、`tests/integration/test_request_conn.py` | ACC-20261101-01 | store、api |
| R24 | 公開匯入路徑與常數不變(admin `__all__`、routing 兩層 `__all__`、domain 常數與 admin `Literal`/schema CHECK 一致、adapter `_seam` 替換點隔離) | `tests/contract/test_public_imports.py`、`tests/contract/test_public_imports_admin.py`、`tests/contract/test_public_imports_routing.py`、`tests/unit/test_domain_constants.py`、`tests/unit/adapters_base/test_api_adapter_seams.py` | ACC-20261101-01 | api、routing、adapters |
| R25 | SQL 只在 `src/spillway/store/`(Global Constraints 定義的 AST 規則);repo 查詢與搬移前 SQL 等價、dashboard 單一 GROUP BY | `tests/contract/test_layering.py`、`tests/unit/store/test_repo_queries.py` | ACC-20261101-01 | store |
| R26 | 驗收追蹤補測:keyring 端到端(記憶體後端)、DELETE 連帶刪除與孤兒列、migration 表名衝突回滾(含最後一張表)、AC-4 無 seed 情境、`_AdminRoute` 在無 app handler 下的 422/404 | `tests/integration/test_keyring_credential.py`、`tests/integration/admin/test_admin_delete_cascade.py`、`tests/unit/store/test_migration_nonempty.py`、`tests/integration/admin/test_admin_route_class.py` | ACC-20261101-01 | ledger、store、api、routing |
| R27 | schema v1→v2 migration:備份一次且不覆寫(已存在加時間戳記後綴)、外鍵孤兒先擋且不備份、鎖被占不 migrate、失敗回滾、WAL 一併備份、`cooldowns` reason 由帳本推得、暫行預算列保留 | `tests/unit/store/test_migration_v1_v2.py`、`tests/unit/store/test_repo_cooldowns.py`、`tests/integration/test_init_token.py` | ACC-20261101-02 | store、cli |
| R28 | CliRunner:新 session、獨立 process group,逾時與取消殺整個 group(含孫行程;SIGTERM 不理會時 SIGKILL)、輸出上限截斷、名額 `try_acquire`/`release`、spawn 視窗停機不漏殺;子行程 API 只在 adapters.base;主行程結束以 `returncode` 判斷,孫行程持有管線時於 `drain_timeout`(加 `term_grace`)內返回(3.11–3.13;`Process.wait` 被 monkeypatch 為不返回時亦然) | `tests/unit/adapters_base/test_cli_runner.py`、`tests/contract/test_layering.py` | ACC-20261101-02 | adapters、安全 |
| R29 | claude_cli / codex_cli:決策 9 env 移除與 `SPILLWAY_DEPTH=1`、`--strict-mcp-config` 與 `-c mcp_servers={}` 不可被 `extra_args` 移除或覆寫、`--` 在 prompt 前、`-o` 檔必刪、上游輸出分類、codex `cost_usd` NULL、`tokens_in` 含 cache 欄、params.timeout 共用驗證 | `tests/unit/adapters/test_claude_cli.py`、`tests/unit/adapters/test_codex_cli.py`、`tests/contract/test_call_status.py` | ACC-20261101-02 | adapters、安全 |
| R30 | 假 CLI 與真實 CLI 護欄:pytest、behave、子行程三處把真實 claude/codex 移出 PATH;`~`、相對、空 PATH 項移除;控制目錄消失時假 CLI 自行結束。須另在 PATH 含真實 claude/codex 的環境執行一次仍通過 | `tests/fakes/test_cli_upstream.py` | ACC-20261101-02 | 測試基礎、安全 |
| R31 | Router 併發與停機:`concurrency_full` 篩除且不寫帳本、取不到名額換下一位、`shutting_down` 不開始新嘗試、取消時記 `error="shutdown"`、`EXCLUDE_REASONS` 值快照、管理 API test-call 走同一守門 | `tests/unit/routing/test_router.py`、`tests/unit/routing/test_router_cli_slots.py`、`tests/integration/test_router_cli_concurrency.py`、`tests/unit/routing/test_filter.py`、`tests/contract/test_exclude_reasons.py`、`tests/integration/admin/test_admin_deployments.py` | ACC-20261101-02 | routing、api(admin) |
| R32 | token 與 init:只存 SHA-256、`init` 冪等與 `--rotate`、鎖被占時拒絕、首次 serve stderr 一次、rotate 回 `{"token"}` 加 `Cache-Control: no-store`、明文不入日誌與帳本;serve 前置檢查(埠、鎖)失敗不 migrate | `tests/integration/test_init_token.py`、`tests/integration/admin/test_admin_token_rotate.py`、`tests/unit/test_settings.py` | ACC-20261101-02 | cli、api(admin)、安全 |
| R33 | graceful shutdown(子行程 serve):SIGTERM 等滿寬限後殺 CLI group 並記 shutdown;雙 SIGINT 與 SIGTERM→SIGINT 快速結束且無殘留。**須前景執行**(shell `&` 背景化會讓 SIGINT 被忽略而失敗) | `tests/integration/test_graceful_shutdown.py` | ACC-20261101-02 | cli、安全 |
| R34 | M3a Acceptance operation 自動部分;`artifacts/m3a/http-log.jsonl` 只含四鍵、`sk-` 命中 0 | `tests/acceptance/test_m3a_operation.py` | ACC-20261101-02 | api、routing、cli、安全 |
| R35 | m3a 行為情境:AC-1、AC-2、AC-12、AC-16/AC-17 CLI 版、NFR-6、codex 狀態、子行程環境與隔離旗標、chat route 不選 CLI | `tests/features/m3a_cli.feature` | ACC-20261101-02 | routing、quota、adapters |
| R36 | M3b Acceptance operation 自動部分(認證與工具、list_changed、stdio、停機四函式);artifacts/m3b/http-log.jsonl 只四鍵、含 /mcp 列、全目錄 token 0 命中。**須前景執行** | `tests/acceptance/test_m3b_operation.py` | ACC-20261102-01 | api(mcp)、cli、安全 |
| R37 | /mcp 驗證順序與錯誤碼(depth 先於 token、任意長度 depth 403、未產生 token 401、停機 503、群組存在性、session 綁群組、token 不入日誌);SDK root logger 紀錄不外洩 prompt | `tests/integration/mcp/test_mcp_auth.py`、`tests/integration/mcp/test_mcp_log_leak.py` | ACC-20261102-01 | api(mcp)、安全 |
| R38 | 全端點 Host/Origin(純 ASGI 最外層、拒絕 403 不 raise、websocket 1008、GET 不變式涵蓋全部路由與 Mount、Origin 比對綁定埠) | `tests/integration/test_host_origin.py` | ACC-20261102-01 | api、安全 |
| R39 | MCP 工具 ask/list_routes(與 Router 同一 OrderPlanner.plan)/quota_status(與 dashboard 同源)、mcp_catalog 與 tools/list 逐欄相同 | `tests/integration/mcp/test_mcp_tools.py`、`tests/integration/mcp/test_mcp_catalog.py`、`tests/unit/test_quota_rows.py` | ACC-20261102-01 | api(mcp)、routing、quota |
| R40 | tools/list_changed(commit 後通知全部 session)與 MCP 停機(只關 GET 串流、D31 四步驟順序、drain 期滿 ask → shutting_down)、SDK 私有屬性絆線 | `tests/integration/mcp/test_mcp_list_changed.py`、`tests/integration/mcp/test_mcp_shutdown.py`、`tests/integration/mcp/test_sdk_compat.py`、`tests/unit/api/test_drain.py` | ACC-20261102-01 | api(mcp)、安全 |
| R41 | spillway mcp-stdio:結束碼 0/1/2/3/130、錯誤帶 data.code、stdout 只 JSON-RPC、不經代理、stderr 無 token/prompt/traceback、HTTP 端結束即退出、stdin EOF 等待 | `tests/integration/test_mcp_stdio.py` | ACC-20261102-01 | cli、安全 |
| R42 | GET /api/mcp/config:5 種工具 variants 可解析、以 SPILLWAY_TOKEN 參照、無明文、stdio 絕對 command + --port、惡意群組名不注入、404/422 | `tests/integration/admin/test_admin_mcp_config.py`、`tests/contract/test_public_imports_admin.py` | ACC-20261102-01 | api(admin)、安全 |
| R43 | 停機期滿 503 JSON shutting_down(/v1)、Drained 子 task 於 lifespan 前被等待、Ctrl-C 結束碼 130 無 traceback。**須前景執行** | `tests/integration/test_graceful_shutdown.py` | ACC-20261102-01 | cli、安全 |
| R44 | adapters.base.cli_common(工作目錄、截斷、整數解析三處合一;adapter 層 cwd 失敗回 error) | `tests/unit/adapters_base/test_cli_common.py`、`tests/unit/adapters_base/test_cli_cwd_adapters.py` | ACC-20261102-01 | adapters |
| R45 | m3b 行為情境 9 個(群組端點、ask、list_routes、token、遞迴、list_changed、Host/Origin、設定輸出、停機) | `tests/features/m3b_mcp.feature` | ACC-20261102-01 | api(mcp)、routing、安全 |
| R46 | M4a Acceptance operation 自動部分(往返逐位元組、黃金檔設定表快照、merge、dry_run 與 privileged、replace 備份與還原、422/409、CSV、總覽、群組名稱、stdio SDK 503);http-log 只四鍵、全目錄假金鑰與 token 命中 0。須前景執行 | `tests/acceptance/test_m4a_operation.py` | ACC-20261102-02 | api(admin)、cli、安全 |
| R47 | 設定檔格式與往返(黃金檔、逐欄省略、行分隔字元往返、configio 不 import api 層) | `tests/unit/test_configio_dump.py`、`tests/unit/data/config_golden.yaml` | ACC-20261102-02 | api(admin)、store |
| R48 | 匯入邊界與套用(嚴格 loader、yaml 型別、1 MiB、跨站 403、單一交易、備份規則、dry_run、privileged、replace 前置可還原檢查、健康失效與 list_changed、與管理 API 規則一致) | `tests/unit/test_configio_parse.py`、`tests/integration/admin/test_admin_config_io.py`、`tests/contract/test_import_admin_parity.py` | ACC-20261102-02 | api(admin)、安全、store |
| R49 | 帳本 CSV(欄位、BOM、公式前置、10 萬列上限、generator 連線生命週期與中斷) | `tests/integration/admin/test_admin_ledger_csv.py` | ACC-20261102-02 | api(admin)、安全、store |
| R50 | 設定總覽(形狀、first_run、token_issued、version、data_dir 來源) | `tests/integration/admin/test_admin_settings_overview.py` | ACC-20261102-02 | api(admin) |
| R51 | 群組名稱規則與既有名稱可用;MCP 設定輸出無頂層相容欄位 | `tests/integration/admin/test_admin_groups.py`、`tests/integration/admin/test_admin_mcp_config.py` | ACC-20261102-02 | api(admin)、安全 |
| R52 | mcp-stdio 錯誤對應成對(9.1.2 body 才以 3 結束)、測試用 session 上限、環境變數名稱常數契約 | `tests/integration/test_mcp_stdio.py`、`tests/contract/test_env_names.py` | ACC-20261102-02 | cli、api(mcp)、安全 |
| R53 | 測試輸出遮罩 token(最長優先、裸字串) | `tests/unit/test_helpers.py` | ACC-20261102-02 | 測試基礎、安全 |
| R54 | m4a 行為情境 10 個 | `tests/features/m4a_config.feature` | ACC-20261102-02 | api(admin)、安全 |
| R55 | Web UI 畫面測試全部(10 頁、Playwright headless Chromium 1248;瀏覽器不存在時失敗不跳過;console error、pageerror、非本機請求皆判失敗;含精靈續作路徑 409 沿用警告) | `tests/ui/`(含 `tests/ui/test_wizard.py`) | ACC-20261103-01 | ui、安全 |
| R56 | m4b 行為情境 10 個(AC-6、AC-8、AC-10、AC-20、AC-21、AC-22、NFR-4 畫面部分、拖曳、匯入確認、純文字顯示) | `tests/features/m4b_ui.feature` | ACC-20261103-01 | ui、安全 |
| R57 | M4b Acceptance operation 自動部分 ①–⑩(精靈、連動選單、拖曳後 /v1 改由新首位處理、AC-10 經畫面往返逐位元組、預覽為 dry_run 且資料不變、AC-21、AC-22、token 一次顯示);NFR-4 對資料庫、匯出檔、CSV、備份、日誌、全部畫面 HTML 與 API 回應命中 0;產物 artifacts/m4b/。須前景執行 | `tests/acceptance/test_m4b_operation.py` | ACC-20261103-01 | ui、api(admin)、安全 |
| R58 | 靜態檔提供與安全標頭(/ 302、/ui 過濾點檔與副檔名與結尾斜線、CSP 含 Trusted Types、/api no-store)與前端靜態掃描(禁用 HTML 插入與外部網址、導覽一致、spillway.web 只 import 標準庫與 starlette、package-data) | `tests/integration/test_web_static.py`、`tests/unit/test_web_assets.py` | ACC-20261103-01 | ui、安全 |
| R59 | 9.2.8 新端點與欄位:POST /api/groups/preview、GET /api/routes/{name}/effective-order(與 MCP list_routes 相等)、/api/mcp/catalog、daily_spend_7d、min_depth(含 CSV 上限)、版本字串單一來源 | `tests/integration/admin/test_admin_groups.py`、`tests/integration/admin/test_admin_routes.py`、`tests/integration/admin/test_admin_mcp_catalog.py`、`tests/integration/admin/test_admin_dashboard.py`、`tests/integration/admin/test_admin_ledger.py`、`tests/integration/admin/test_admin_ledger_csv.py`、`tests/integration/mcp/test_mcp_version.py` | ACC-20261103-01 | api(admin)、api(mcp) |
| R60 | 匯入 privileged 含既有成員 provider 變更 | `tests/integration/admin/test_admin_config_io.py` | ACC-20261103-01 | api(admin)、安全 |
| R61 | 小型整理行為不變:422 建構函式、時間戳記 helper、read_tx 巢狀 | `tests/unit/api/test_admin_invalid.py`、`tests/unit/test_clock.py`、`tests/unit/store/test_read_tx.py` | ACC-20261103-01 | api(admin)、store |
| R62 | 共用前端模組行為(常駐錯誤、latest()、fmtUSD、tagInput、fillGroupSelect、首次導向 replace 與上一頁) | `tests/ui/test_shell.py` | ACC-20261103-01 | ui |
| R63 | 雜湊 lock 檔契約(確切版本與 sha256、全檔無註解、涵蓋 pyproject 相依、兩份 lock 版本一致、runtime ⊆ dev、litellm 排除已知被植入版本、build-constraints 只有 setuptools) | `tests/contract/test_dependency_lock.py` | ACC-20261107-01 | 相依、安全 |
| R64 | venv 與 dev lock 的漂移偵測(lock 外套件、版本不符、未安裝、重複 dist-info) | `tests/unit/test_venv_matches_lock.py` | ACC-20261107-01 | 相依 |
| R65 | venv 入侵檢查腳本(.pth 內容與歸屬、啟動掛鉤各種形式、RECORD 雜湊與遞迴歸屬、未加 -S 拒絕執行) | `tests/unit/test_check_venv_integrity.py`、`scripts/check_venv_integrity.py` | ACC-20261107-01 | 相依、安全 |

## Interaction spec(M2)

| # | 指令 | 產物 | 來源 ACC | 範圍標籤 |
|---|------|------|----------|----------|
| R17 | `python -m pytest tests/integration -q --junitxml=artifacts/m2/integration.xml` | integration.xml,failures=0、errors=0 | ACC-20261031-01 | api |
| R18 | `python -m pytest tests/acceptance/test_operation.py -q` | http-log.jsonl,每行只有 method、path、status、elapsed_ms,`sk-` 命中 0 | ACC-20261031-01 | api、安全 |
| R19 | `python -m pytest tests/contract -q --junitxml=artifacts/m2/contract.xml` | contract.xml,failures=0、errors=0 | ACC-20261031-01 | adapters |

產物目錄 artifacts/m2/(與 CHG-20261101-02 的 artifacts/m2c/)不進版控,每次執行重新產生。

## 不在迴歸集內的條件

- 擁有者真實冒煙(Guideline Q1):人工動作,每個里程碑閘門由擁有者以真實帳號執行一次,結果記入該里程碑的 ACC。
- Linux、Windows:未涵蓋(Guideline Q2),待加上 CI matrix 後補登記。

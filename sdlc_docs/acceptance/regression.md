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
| R1 | `python -m pytest -q` | exit 0;0 failed(ACC-20261031-01 時為 1089 passed) | 單元、合約、整合、fakes 與 acceptance 全部;含 AC-19 合約測試、管理 API 錯誤碼(FR-13、FR-25)、`stream: true` 400、retention 每小時排程、AC-7、NFR-1、NFR-2、NFR-3、NFR-5 | ACC-20261031-01 | 全部 |
| R2 | `python -m behave tests/features` | exit 0;5 features、18 scenarios、155 steps 全過、0 skipped | AC-3、AC-4、AC-5、AC-13、AC-14、AC-15、AC-18、AC-19、4.2.5(api 版)、4.2.6(api 版) | ACC-20261031-01 | routing、quota、estimate、ledger、redact、adapters、api |
| R3 | `python -m pytest tests/acceptance -q -rs` | 9 passed 且**必須 0 skipped**(缺 lsof 時 NFR-3 與操作驗收會被跳過;驗收平台 macOS 有 lsof,出現 skipped 即視為未通過) | AC-7、NFR-1(dry-run P95 < 20 ms)、NFR-2(dashboard P95 < 200 ms)、NFR-3(只聽 127.0.0.1)、NFR-5(payload 過期刪除)、Acceptance operation、NFR-4 預掃描(不計入 M2 閘門) | ACC-20261031-01 | api、cli、routing、ledger、store |

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

## Interaction spec(M2)

| # | 指令 | 產物 | 來源 ACC | 範圍標籤 |
|---|------|------|----------|----------|
| R17 | `python -m pytest tests/integration -q --junitxml=artifacts/m2/integration.xml` | integration.xml,failures=0、errors=0 | ACC-20261031-01 | api |
| R18 | `python -m pytest tests/acceptance/test_operation.py -q` | http-log.jsonl,每行只有 method、path、status、elapsed_ms,`sk-` 命中 0 | ACC-20261031-01 | api、安全 |
| R19 | `python -m pytest tests/contract -q --junitxml=artifacts/m2/contract.xml` | contract.xml,failures=0、errors=0 | ACC-20261031-01 | adapters |

產物目錄 artifacts/m2/ 不進版控,每次執行重新產生。

## 不在迴歸集內的條件

- 擁有者真實冒煙(Guideline Q1):人工動作,每個里程碑閘門由擁有者以真實帳號執行一次,結果記入該里程碑的 ACC。
- Linux、Windows:未涵蓋(Guideline Q2),待加上 CI matrix 後補登記。

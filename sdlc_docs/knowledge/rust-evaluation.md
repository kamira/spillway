# Spillway 改用 Rust 評估(DIR-5)

- 日期:2026-11-07(UTC+0);基準:main d86c336(M1–M4 完成)
- 依據:knowledge DIR-5「不限定 python,之後可以改用 rust;先把現在的需求處理完以後再評估 rust,最後才決定是否處理」
- 性質:評估,不改程式;決定由擁有者做
- 擁有者決定(2026-11-07):規劃改寫;四項選擇見 knowledge DIR-5 規則 4 與 CHG-20261107-01 決策 2
- 增補(2026-11-08,CHG-20261108-01):擁有者改為保留 Python 版並新增 Rust 版(雙實作,knowledge DIR-5);本文為 2026-11-07 當時的評估,內文未改。文中「422 訊息格式是契約,需逐字對齊」的說法改依 spec 15.7.3:跨實作只比對欄位路徑集合

## 1. 結論先講

**建議:現在不改寫成 Rust。** 先做低成本的兩件事:鎖定相依版本(含雜湊),並把測試改成「從外部打 HTTP」的黑箱形式。等出現明確的驅動因素再重評,例如要發成單一執行檔,或要完全擺脫 Python。

理由有四:
1. **效能不是瓶頸。** NFR 都有大量餘裕:路由決策 P95 約 12–14 ms(上限 20),dashboard 約 20 ms(上限 200)。這是單人本機工具。
2. **最大的 Python 相依 litellm 在 Rust 沒有對等品。** litellm 一次提供四件事:多供應商呼叫、token 計數、本地價格表、花費計算。Rust 端要自己組。
3. **改寫會碰到一條已確認的規則。** AC-19 與 DIR-3 規則 1 要求「新增一個 AI 只放一個模組資料夾、不改既有檔案、重新啟動即可」。Rust 編譯期註冊做不到,需要另行設計外掛機制,這是擁有者層級的決定。
4. **測試大多綁 Python 內部。** 128 個測試檔中 110 個 import `spillway`,改寫時大部分要重寫。

## 2. 現況量測

| 項目 | 數量 |
|------|------|
| Python 原始碼 | 12,585 行(adapters 2,947、api 4,226、routing 1,444、store 1,280、其他頂層 2,607) |
| 前端靜態檔(HTML/CSS/JS) | 5,480 行,只經 `/api` 溝通,可原樣沿用(決策 5 的目的) |
| 測試 | 37,444 行;128 個測試檔中 110 個 import `spillway` 內部 |
| 可直接沿用的測試資產 | 10 個 feature(60 個情境的 Gherkin 文字)、黃金檔 `config_golden.yaml`、fakes(假上游、假 CLI,為 HTTP/子行程)、7 個以子行程 `serve` 驗收的測試 |
| 執行期相依 | fastapi、uvicorn、litellm、pyyaml、pydantic、platformdirs、keyring、httpx、mcp、jinja2、sse-starlette |

## 3. 逐項對照

| Python 元件 | 用途 | Rust 對應 | 風險 |
|-------------|------|-----------|------|
| FastAPI + uvicorn + Starlette | HTTP、路由、中介層、靜態檔 | axum + tokio + tower-http(ServeDir) | 低 |
| pydantic | 請求驗證與 422 欄位路徑訊息 | serde + 自寫驗證 | 中:422 訊息格式是契約,需逐字對齊 |
| **litellm** | `acompletion` 多供應商呼叫、`token_counter`、`cost_per_token`、`completion_cost`、本地價格表、`reasoning_effort` 轉換 | genai crate(OpenAI、Anthropic、Gemini、DeepSeek、Ollama 等),或自寫各供應商 HTTP client;價格表可沿用 litellm 的 JSON(MIT);token 計數用 tiktoken-rs,其他供應商近似 | **高**:四項功能要自己組,估算(AC-16、AC-18)的精確度會變 |
| mcp(官方 Python SDK) | `/mcp/<group>` Streamable HTTP、session、`tools/list_changed`、`mcp-stdio` 橋接 | rmcp(官方 Rust SDK,支援 Streamable HTTP 伺服端) | 中:目前用到 SDK 私有屬性(`sdk_compat`)處理停機與串流,行為要重新對齊 |
| sse-starlette | SSE | axum SSE | 低 |
| SQLite(store) | 帳本、設定、migration | rusqlite 或 sqlx;`schema.sql` 可沿用 | 低 |
| pyyaml(嚴格 loader) | 設定匯入:不允許 alias、深度 32、重複鍵 | saphyr / yaml-rust2 加自寫檢查 | 中:匯出要與黃金檔逐位元組相同(AC-10) |
| jinja2 | MCP 設定輸出模板 | minijinja(相容 Jinja 語法) | 低 |
| keyring、platformdirs | 憑證參照、資料目錄 | keyring、directories crate | 低 |
| CLI 子行程(claude、codex) | 併發上限、逾時、行程群組終止、停機 | tokio::process | 中:行程群組與訊號的邊角行為要重驗 |
| **adapter 自動發現**(AC-19、DIR-3) | 放一個資料夾、重啟即載入新 AI | 編譯期註冊;若要維持「不改既有檔案」需外掛機制(動態函式庫、WASM,或改成設定檔驅動的通用 OpenAI 相容 adapter) | **高**:牽涉已確認的需求,要擁有者決定 |

## 4. 選項

| 選項 | 內容 | 成本 | 效益 |
|------|------|------|------|
| **A. 維持 Python,強化相依** | 鎖定 litellm 等相依的確切版本與雜湊,更新流程改為手動審視後升版(實際採用 `uv pip compile` 產生雜湊 requirements,見 CHG-20261107-01 決策 3) | 小(1 筆 CHG) | 降低供應鏈風險;其他不變 |
| **B. 測試黑箱化**(A 的延伸,也是 C 的前置) | 把整合與行為測試改成經 HTTP 或子行程操作 `serve`,不 import 內部 | 中 | 測試同時適用 Python 與未來的 Rust 實作;改寫時才有安全網 |
| **C. 完整改寫成 Rust** | 以 B 的黑箱測試為驗收基準,逐模組改寫(store → routing → api → adapters → mcp → CLI),前端不動 | 大:原始碼約 1.6–2 萬行 Rust,外加不能沿用的測試;規模約等於 M2–M4 全部 | 單一執行檔、記憶體低、啟動快、完全移除 Python 與 litellm 相依 |
| D. 只把熱路徑改 Rust(Python 外殼) | 類似 litellm 自己的做法 | 中到大 | 對本專案效益小(效能已有餘裕),卻多一層跨語言複雜度;**不建議** |

## 5. 建議的順序

1. **A(建議立即做)**:鎖定相依版本與雜湊。另見第 6 節。
2. **B(建議做)**:不論是否改寫都有價值,而且是 C 的必要前置。
3. **C 先不做**:等出現以下任一驅動因素再重評:要發布成單一執行檔給別人用;要在沒有 Python 的環境執行;litellm 再次出問題且無法以 A 控制;或決定放棄 AC-19 的「放資料夾即可新增 AI」。

若擁有者仍決定改寫,要先回答兩個問題:
- adapter 擴充方式(AC-19、DIR-3 規則 1):外掛機制、設定檔驅動的通用 adapter,或改為「新增 AI 需重新編譯」。
- litellm 的替代:用 genai 這類 crate,或只自寫實際用到的供應商(OpenAI、Anthropic、DeepSeek、Gemini、local)。

## 6. 本次評估順帶發現:litellm 供應鏈事件

- litellm 在 2026 年 PyPI 兩次被植入惡意版本:3 月的 1.82.7、1.82.8,與 10 月 21 日的 1.107.0(Python 啟動時執行的 `.pth`/`sitecustomize` 竊取憑證,上架約 2 小時後移除)。
- **本機不受影響**(查核日 2026-11-07):venv 安裝的是 1.106.6;1.106.6 於 2026-10-28 上架,晚於 2026-10-21 的事件,至今(2026-11-07)未被撤回或標記;uv 快取沒有 1.107.0。以 `python3 -I scripts/check_venv_integrity.py .venv`(CHG-20261107-01 決策 6 ⑤)檢查:`.pth` 只有 `__editable__.spillway-0.1.0.pth`,沒有 `sitecustomize.py`、`usercustomize.py`,全部 RECORD 雜湊相符,site-packages 頂層沒有不屬於任何 RECORD 的項目;結果「沒有發現」(結束碼 0)(以系統 python3、未啟用 venv 執行;腳本之後改為須加 `-S`,見 CHG-20261107-01 施工審議)。
- **處理**:`pyproject.toml` 原本的範圍是 `litellm>=1.50,<2`,任何一次全新安裝都會抓最新版;CHG-20261107-01 改為以雜湊 lock 安裝,並在 `pyproject.toml` 排除三個已知被植入的版本。

## 7. 參考

- [rmcp 3.1.0 README](https://docs.rs/crate/rmcp/3.1.0/source/README.md)、[Model Context Protocol Rust SDK](https://docsearch.algolia.com/mcp/docs/repo/modelcontextprotocol/rust-sdk)
- [genai crate](https://docs.rs/crate/genai)、[genai 0.7.1](https://docs.rs/crate/genai/0.7.1)
- [LiteLLM:Migrating to Rust](https://docs.litellm.ai/blog/tags/performance)、[LiteLLM Rust 遷移](https://docs.litellm.ai/blog/tags/rust-migration)
- [LiteLLM:Oct 21, 2026 v1.107.0 compromised](https://docs.litellm.ai/blog/security-incident-oct-21-2026)、[GitHub issue #47826](https://github.com/BerriAI/litellm/issues/47826)、[Datadog Security Labs](https://securitylabs.datadoghq.com/articles/litellm-compromised-pypi-docker/)、[Microsoft Security Blog](https://www.microsoft.com/en-us/security/blog/2026/10/23/inside-the-litellm-1-107-0-supply-chain-compromise-credential-theft-through-python-startup-hooks/)
- [LiteLLM March 2026 security update](https://docs.litellm.ai/blog/security-update-march-2026)、[GitHub issue #24518](https://github.com/BerriAI/litellm/issues/24518)

# AI Guideline — Spillway

- 專案:spillway
- 分支(Branch):main
- 帳本落點(Ledger root):`sdlc_docs/` ← `docs/` 已存放專案自己的企劃書、規格書與 UI 設計稿,帳本不寫入該目錄
- 版本:v1.10
- 日期:2026-11-08(UTC+0)
- 狀態:已確認(2026-10-29,使用者);v1.5 記錄使用者於 2026-11-01 決定的 M3 拆分(CHG-20261101-03 決策 1);v1.6 記錄使用者於 2026-11-02 提出的 UI-8(FR-26)與 M3b 範圍(CHG-20261102-01);v1.7 記錄使用者於 2026-11-02 決定的 M4 拆分、UI-8 名稱「API 參考」與 M4b 測試方式(CHG-20261102-02 決策 1–3);v1.8 記錄 M4b(CHG-20261103-01):使用者於 2026-11-03 決定的 UI-8 版面與 Playwright 沿用已快取的 Chromium(決策 3、4)、前端為靜態頁經 `/api` 取資料(決策 5)、使用者指示語言不限定 Python(knowledge DIR-5);v1.9 記錄使用者於 2026-11-07 依 DIR-5 完成 Rust 評估後決定規劃改寫(CHG-20261107-01 決策 2);v1.10 記錄 M5 進度:測試黑箱化分兩筆 CHG(CHG-20261107-03 決策 4)
- Skill:ai-sdlc v1.69

## 0. 需求來源與優先序

| 來源 | 內容 | 角色 |
|------|------|------|
| `docs/spec.md`(2026-10-29;修訂至 2026-11-03) | 資料模型、路由、額度、effort、adapter、MCP、HTTP API、UI 清單(8 個畫面)、NFR-1–7、AC-1–22 | **規範來源**:條文編號(如 4.2.5)為需求的正式依據 |
| `docs/proposal.md`(2026-10-29) | 背景、目標 G1–G7、範圍、選型、合規邊界、里程碑、風險 | 目的與範圍;與規格書衝突時以規格書為準(企劃書第三節自述) |
| `docs/ui/*.dc.html` | UI-1–UI-7 的 layout(共 8 個畫面;UI-8 沒有 layout 稿,版面已由使用者決定:頂端群組選單、左側可折疊索引、右側內容,CHG-20261103-01 決策 4) | UI-1–UI-7 的版面依據;畫面數值為示意;實作與設計稿的差異見 design.md「設計差異」 |

本 Guideline 不複寫規格條文,只做索引、範圍界定、補足規格書未定的決策。規格書條文若要修改,走 modification-guide(CHG),並同步本文件升版。

## 1. 背景與目標

單人本機使用的 LLM 路由閘道。訂閱 CLI、雲端 API key、local 模型分散計費,沒有單一入口決定「這次用哪個模型」,導致訂閱附贈額度用不完時仍在付 API 費用。Spillway 對外只暴露 route 名稱,依使用者排定的 slot 順序(預設「訂閱 → API → local」)選有額度的成員,並顯示各成員剩餘額度。

成功定義 = 企劃書目標 G1–G7 全數由規格書第 12 節的驗收項(AC-1–22)通過證明。

## 2. 範圍

### 納入(依里程碑,出自企劃書第八節)

| 里程碑 | 內容 | 閘門 |
|--------|------|------|
| M1 規格定案 | 企劃書、規格書、UI layout | 規格審閱通過(**已完成**,文件已在 `docs/`) |
| M2 路由核心 | SQLite schema 與帳本、路由器與額度計算、LiteLLM adapter(api、local)、OpenAI 相容 HTTP API、管理 API | 路由測試全過(G3、G4) |
| M3 CLI 與 MCP | 拆為兩筆 CHG(使用者決定 2026-11-01):**M3a**(CHG-20261101-03)claude-cli、codex-cli adapter 與子行程執行器 CliRunner、CLI 併發上限、schema v2(冷卻獨立成 `cooldowns` 表)與 v1→v2 migration、存取 token 與 `spillway init`、graceful shutdown;**M3b**(CHG-20261102-01)MCP server(Streamable HTTP + stdio 橋接 `mcp-stdio`)、MCP 設定輸出、全端點 Host/Origin 檢查——**已完成**(自動化驗收與擁有者真實冒煙皆通過,ACC-20261102-01) | 4 種工具可連線(G1、G6);閘門於 M3b 結束時驗收(已通過) |
| M4 Web UI | 拆為兩筆 CHG(使用者決定 2026-11-02,CHG-20261102-02 決策 1):**M4a**(CHG-20261102-02,後端)設定匯入匯出(含 `dry_run`、`privileged`、replace 自動備份)、帳本 CSV 匯出、設定總覽 `GET /api/settings`、群組名稱規則與 M3b 帶入項;**M4b**(CHG-20261103-01,本筆)8 個畫面(UI-1–UI-8)、首次設定精靈畫面、M4a 帶入項(畫面需要的讀取端點與小型整理),畫面以 Playwright 自動測試(dev 相依 `playwright==1.64.1`,沿用已快取的 Chromium 1248、不下載瀏覽器;CHG-20261103-01 決策 3) | 空 DB 首呼 ≤ 10 分(G2、G5、G7);閘門於 M4b 結束時驗收 |
| M5 Rust 改寫(規劃中) | 使用者 2026-11-07 依 DIR-5 完成評估(`sdlc_docs/knowledge/rust-evaluation.md`)後,決定規劃改寫;四項選擇與順序見 knowledge DIR-5 規則 4。順序:CHG-20261107-01 鎖定相依 → 測試黑箱化(另開 CHG)→ 逐模組移植(另開 CHG,可再拆)→ Rust 版通過同一套測試後,Python 版退役。改寫 CHG 開立前,現行選型(§6)不變。**進度**:鎖定相依已完成(CHG-20261107-01,ACC-20261107-01);測試黑箱化分兩筆 CHG(CHG-20261107-03 決策 4)——**黑箱化(一)**(CHG-20261107-03,本筆):測試控制介面 `/__test__/*`(僅測試模式)、行程契約(spec 13、14)、資料庫宣告為跨語言契約、黑箱 harness `tests/blackbox/`,轉換 behave、acceptance 與畫面測試;**黑箱化(二)**(另開 CHG):HTTP 層的 integration 測試與剩餘的行程內輔助(範圍見 CHG-20261107-03「後續 CHG」) | 各 CHG 開立時定;至少要求黑箱測試在 Python 版全綠,且 Rust 版通過同一套測試 |

### 不納入(明確排除)
- 多使用者、權限角色、帳號共享、對外開放的服務(spec 1.1.1)。
- 讀取、轉發訂閱 OAuth token,或模擬官方客戶端請求格式(spec 1.1.2)。
- 同一家多個訂閱帳號的輪替或集中;訂閱 CLI 模組宣告 single,同 provider 至多 1 個成員(FR-25)。
- Streaming 回應(spec 1.1.3,列為後續項目)。
- 模型訓練、微調、向量庫。
- Gemini 訂閱 CLI 成員(企劃書第七節標為「待確認條款」,規格書未定義其 adapter)。Gemini CLI 作為 **MCP 設定輸出對象**仍在範圍內(G6)。

## 3. 利害關係人

| 角色 | 關注點 |
|------|--------|
| 擁有者(Weiss Lin):唯一使用者、管理者、需求決策者 | 訂閱額度用足、額度可見、設定可重建、不違反各家政策 |
| 上游供應商(Anthropic、OpenAI、其他 API、local 模型) | 只經官方 CLI 或官方 API 呼叫;CLI 參數與政策可能變動 |
| AI 開發 agent | 依本文件與規格書實作;不自行擴張範圍 |

## 4. 功能需求

| 編號 | 需求 | 規格依據 | 里程碑 | 優先級 |
|------|------|----------|--------|--------|
| FR-1 | SQLite 資料庫:WAL、外鍵、UTC 時間、憑證只存參照;初始化只建表 | 2.1、2.3 | M2 | P0 |
| FR-2 | 請求解析:`local/` 直呼、route 名稱、`model_name` 隱含 route、404 | 4.1 | M2 | P0 |
| FR-3 | 候選篩選:啟用、能力(ctx/tools/vision)、mode、額度與冷卻、local 健康檢查 | 4.2.1–4.2.4 | M2 | P0 |
| FR-4 | 預估額度檢查(route 勾選時)與未勾選時的冷卻自動恢復 | 4.2.5、4.2.6 | M2 | P0 |
| FR-5 | 順序決定:手動 slot 優先、預設層級、未排序新成員、還原預設 | 4.3 | M2 | P0 |
| FR-6 | Slot 內加權隨機選擇(權重 × 剩餘比例)、失敗重選、local 依 position | 4.4 | M2 | P0 |
| FR-7 | 失敗處理與帳本:每次嘗試一列、quota/429/error 處置、503 `no_candidate` | 4.5 | M2 | P0 |
| FR-8 | 額度計算:四種 window、各類型花費來源、上游訊號判定(可設定)、手動清除冷卻 | 5.1–5.3 | M2(cli 部分 M3) | P0 |
| FR-9 | Token 與花費預估,並露出於 dry-run、帳本、header、MCP | 5.4 | M2(MCP 部分 M3) | P1 |
| FR-10 | Effort 統一五級、來源優先序、降級、各 adapter 對應 | 6 | M2(cli 部分 M3) | P0 |
| FR-11 | Adapter 模組化:一種 AI 一個子套件(manifest + 可用模型清單),啟動時自動發現;新增 AI 不修改任何既有檔案;共用合約測試 | 7.1、7.6.1–7.6.3、7.6.6–7.6.8;design D11 | M2 | P0 |
| FR-12 | LiteLLM adapter(api、local) | 7.2 | M2 | P0 |
| FR-13 | OpenAI 相容 HTTP API 與管理 API(含 dry-run、estimate、ledger、order) | 9 | M2 | P0 |
| FR-14 | Context 記錄:全域/route 開關、截斷、金鑰遮罩、每小時清理、保存天數 | 2.4 | M2 | P1 |
| FR-15 | claude-cli adapter:清除 API key 環境變數、深度標記、stdin prompt、JSON 解析 | 7.3、7.5 | M3 | P0 |
| FR-16 | codex-cli adapter | 7.4、7.5 | M3 | P0 |
| FR-17 | CLI 成員並行上限(預設 1,超過視為不可用) | NFR-6 | M3 | P1 |
| FR-18 | MCP server:每群組端點、Bearer token、`ask`/`list_routes`/`quota_status`、list_changed、遞迴防護 | 8.1 | M3 | P0 |
| FR-19 | MCP 設定輸出:Claude Code、Codex、Cursor、Gemini CLI、通用 stdio;模板檔維護 | 8.2 | M3 | P0 |
| FR-20 | Web UI 7 個畫面(UI-1–UI-7)與拖曳規則;畫面已實作(CHG-20261103-01,靜態頁經 `/api` 取資料;閘門見第 7 節) | 10 | M4b | P0 |
| FR-21 | YAML 設定匯入(merge/replace、單一交易、`dry_run` 預覽、replace 前自動備份、回應 `privileged`)與匯出(不含憑證、不含 payload) | 3、9.2 | M4a | P0 |
| FR-22 | 首次設定精靈(後端資料來源 `GET /api/settings` 的 `first_run`、`token_issued`、`counts` 屬 M4a;畫面屬 M4b,已實作為獨立頁 `/ui/wizard.html`:成員 → 測試呼叫 → route → 完成,CHG-20261103-01 決策 18、25) | 9.2.7、10(UI-7) | M4a(後端)、M4b(畫面) | P1 |
| FR-23 | 每個 adapter 模組可設定可用模型清單(`models.yaml`,local 可另查上游);能力預設跟著模型走 | 7.6.4;design D12 | M2 | P0 |
| FR-24 | 後台以連動選單選取 kind → provider → model,effort 選單只列模型支援的等級;資料來自 `GET /api/adapters`;保留「其他(手動輸入)」;畫面已實作(UI-2、UI-4,CHG-20261103-01) | 9.2.2、10.2;design D13 | M2(端點)、M4b(畫面) | P0 |
| FR-25 | 訂閱制 CLI 與 API 為不同模組;模組宣告 `cardinality`(single/multiple),建立成員與匯入時強制;內建模組 claude_cli、codex_cli(single)、anthropic_api、openai_api、litellm(multiple) | 2.1.4、7.6.5、7.6.9、9.2.3;design D14、D15 | M2(api 與 litellm)、M3(CLI) | P0 |
| FR-26 | Web UI「MCP 介面目錄」畫面 UI-8(標籤名稱「**API 參考**」,使用者 2026-11-02 決定,CHG-20261102-02 決策 2):**只列 MCP 介面**(連線、工具、通知、錯誤碼四大類,可折疊索引),不列前後端之間的 `/api/*` 與 `/v1`;內容於執行期由 `spillway.api.mcp.mcp_catalog(app, group)` 產生、與程式一致;M3b 只提供資料來源;畫面已於 M4b 實作(CHG-20261103-01,資料經 `GET /api/mcp/catalog`,版面為群組選單 + 可折疊索引 + 內容窗格)。FR-20 維持 UI-1–UI-7,UI-8 只由本條描述 | 10(UI-8)、AC-22;design D37、D43 | M4b | P1 |

## 5. 非功能需求

以規格書第 11 節 NFR-1–7 為準,摘要:

| 類別 | 要求 | 依據 |
|------|------|------|
| 效能 | 路由決策 P95 < 20 ms;`/api/dashboard` P95 < 200 ms(100 成員、10,000 列帳本) | NFR-1、NFR-2 |
| 安全 | 預設只綁 127.0.0.1;資料庫、匯出檔、日誌無金鑰明文;`usage_ledger` 不存內容,payload 過期 1 小時內刪除 | NFR-3、NFR-4、NFR-5 |
| 並行 | CLI 成員同時執行數上限預設 1,超過不排隊 | NFR-6 |
| 相容性 | Python 3.11+;macOS、Linux、Windows | NFR-7(驗收只在 macOS,見第 6 節 Q2) |
| 可維護性 | 新增供應商只加 adapter 檔;新增 MCP 設定輸出的工具只加模板檔與 `TOOL_FORMATS` 一行;CLI 參數、上游訊號規則放在成員設定 | 7.1.2、8.2.3、5.3.1、6.3.2 |

## 6. 限制與假設

- 限制:技術選型 Python + FastAPI + LiteLLM SDK + SQLite(WAL)(企劃書第六節)。前端為靜態頁加少量 JavaScript,經 `/api` 取資料(企劃書第六節授權由規格書定案;spec 10),不引入前端建置流程;語言不限定 Python,見 DIR-5。
- 限制:遠端為公開 repo https://github.com/kamira/spillway(`origin`,預設分支 main);目前無 CI。
- 限制:威脅模型僅擁有者本人本機使用(knowledge DIR-2)。
- 假設:企劃書與規格書衝突時以規格書為準(例:負載平衡以 slot 為單位,而非企劃書寫的「訂閱層與 API 層內部」)。
- 假設:claude-cli 的 effort 參數名稱、CLI 輸出格式於實作當時以已安裝版本確認(spec 6.3.2、8.2.4)。
- 已確認(2026-10-29,使用者):
  - Q1 上游驗證策略:自動測試以**假上游**執行——假的 `claude`、`codex` 執行檔與假 API 回傳規格書定義的輸出、錯誤與訊號(5.3.1),可重複執行;額度用完、429、重置後恢復只在假上游驗證。每個里程碑閘門另由擁有者以真實帳號手動執行一次**真實冒煙**(含 AC-9 接 4 種工具),結果記入該 ACC。
  - Q2 平台驗收:驗收只在 macOS 執行;Linux、Windows 在 ACC 明標「未涵蓋」,待加上 CI matrix 後補驗。程式仍依 NFR-7 寫成跨平台(路徑、子行程、keyring)。
- 待確認:無。

## 7. 驗收條件

以規格書第 12 節為準,依里程碑分組:

- [ ] M2 閘門:AC-3、AC-4、AC-5、AC-7、AC-13、AC-14、AC-15、AC-18、AC-19 通過;NFR-1、NFR-2、NFR-3、NFR-5 量測達標。4.2.5、4.2.6 的邏輯先以 api 成員驗證。
- [x] M3 閘門(ACC-20261102-01,2026-11-02 通過;M3a 冒煙見 ACC-20261101-02):AC-1、AC-2、AC-9、AC-11、AC-12、AC-16、AC-17 通過(AC-1、AC-2、AC-12、AC-16、AC-17 的前置條件用到 claude-sub;AC-11 依 CHG-20261102-01 決策 1 改為直接驗證遞迴防護:`/mcp` 帶 `X-Spillway-Depth: 1` 或 `SPILLWAY_DEPTH=1` 的 `mcp-stdio` 被拒);NFR-6 量測達標。閘門已於 M3b 結束時驗收通過(ACC-20261102-01);M3a 先以假執行檔驗證 AC-1、AC-2、AC-12、AC-16 與 AC-17 的 CLI 版、NFR-6,並由擁有者以真實 claude、codex 各冒煙一次(記入 ACC-20261101-02);AC-9、AC-11 屬 M3b。M3b(CHG-20261102-01)範圍已完成、自動化驗收通過;擁有者已以 4 種工具真實冒煙並記入 ACC-20261102-01,全數通過(AC-9;Gemini CLI 依 UD-1(CHG-20261102-01):其 HTTP headers 路徑失敗時改用 stdio variant,headers 或 stdio 任一成功即算 AC-9 通過,並於 ACC 記錄採用路徑與 headers 結果)。
- [x] M4 閘門:AC-6、AC-8、AC-10、AC-20、AC-21、AC-22 通過;NFR-4 對資料庫、匯出檔、日誌三者搜尋命中數為 0。M4 閘門(含 AC-10、NFR-4)於 M4b 驗收;M4a(CHG-20261102-02)提供 AC-10 的自動化判準(匯出 → 空資料庫 replace 匯入 → 再匯出逐位元組相同)與 NFR-4 的匯出部分。M4b(CHG-20261103-01)以 Playwright(dev 相依 `playwright==1.64.1`,沿用已快取的 Chromium 1248,不下載瀏覽器)自動驗收,AC-8 計時與畫面目視由擁有者手動執行;ACC-20261103-01 通過(含擁有者步驟,2026-11-07),已勾選。
- [ ] 全部:每個閘門在 macOS 通過,並附擁有者的真實冒煙結果(Q1);Linux、Windows 標「未涵蓋」(Q2)。

## 7.1 變更歷程

| 版本 | 日期 | 內容 |
|------|------|------|
| v1.0 | 2026-10-29 | 使用者確認 |
| v1.1 | 2026-10-29 | 結構設計時使用者追加:FR-11 改為 adapter 模組化;新增 FR-23 可用模型清單、FR-24 後台選單 |
| v1.2 | 2026-10-29 | 使用者追加:訂閱 CLI 與 API 為不同模組;模組宣告唯一或多個(FR-25) |
| v1.3 | 2026-10-29 | CHG-20261029-01:FR-11、FR-23–25 已寫入規格書,改引條文編號;AC-19 列入 M2、AC-20–21 列入 M4 |
| v1.4 | 2026-10-29 | CHG-20261029-02:已推上公開 repo kamira/spillway;CI 仍未建立 |
| v1.5 | 2026-11-01 | CHG-20261101-03(使用者決定 2026-11-01,決策 1):M3 拆為 M3a(CLI 成員、schema v2、存取 token 與 init、graceful shutdown)與 M3b(MCP server、`mcp-stdio`、設定輸出);M3 閘門仍於 M3b 結束時驗收 |
| v1.6 | 2026-11-02 | CHG-20261102-01:新增 FR-26(UI-8,使用者 2026-11-02 提出;只限 MCP 介面、名稱待定),FR-20 維持 UI-1–7;畫面數改為 8、AC 範圍改為 AC-1–22、M4 閘門加 AC-22;M3 列標 M3b 範圍完成(閘門待擁有者冒煙後打勾),AC-11 括註依新文字改寫 |
| v1.7 | 2026-11-02 | CHG-20261102-02(使用者決定 2026-11-02,決策 1–3):M4 拆為 M4a(後端:設定匯入匯出、帳本 CSV、設定總覽、帶入項)與 M4b(畫面、首次設定精靈畫面、M4 閘門);FR-21 標 M4a、FR-22 後端標 M4a;FR-26 名稱定案「API 參考」;M4 閘門(含 AC-10、NFR-4)於 M4b 驗收;M4b 以 Playwright(dev 相依,下載 Chromium 前另徵同意) |
| v1.8 | 2026-11-03 | CHG-20261103-01(M4b):M4 列標 M4b 為本筆;UI-8 版面已決定(決策 4);前端限制改為靜態頁經 `/api` 取資料(企劃書第六節授權由規格書定案;spec 10);FR-20、FR-22、FR-24、FR-26 標畫面已實作;Playwright 沿用已快取的 Chromium 1248(決策 3);新增「M4 之後:評估改用 Rust」(使用者 2026-11-03 指示,knowledge DIR-5);M4 閘門待 ACC-20261103-01 通過後勾選 |
| v1.9 | 2026-11-07 | CHG-20261107-01:「M4 之後」改為 M5 Rust 改寫(規劃中);記錄使用者 2026-11-07 的決定(四項選擇與順序,knowledge DIR-5 規則 4) |
| v1.10 | 2026-11-08 | CHG-20261107-03:M5 列補進度——鎖定相依已完成;測試黑箱化分兩筆,本筆為黑箱化(一)(控制介面、harness,轉換 behave、acceptance、畫面測試),integration 於黑箱化(二) |

## 8. AI 開發約定

- 治理帳本:`sdlc_docs/`(changes/、acceptance/、structure/、knowledge/);入口為 root `AGENTS.md`。
- 需求依據:實作與驗收引用 `docs/spec.md` 的條文編號;規格書不在 CHG 外被修改。
- Adapter 模組化:新增一種 AI 只新增 `src/spillway/adapters/<module>/`,不改既有檔案(knowledge DIR-3)。
- 每個里程碑拆成一筆或多筆 CHG;每個 commit 訊息帶 CHG 編號。
- 審議機制:最簡化——審議席只給程式碼位置與意見,由獨立審議席統整權衡(knowledge DIR-1)。
- 安全審議:有限範圍,不考慮外部有權限者(knowledge DIR-2)。規格書內的安全功能(遮罩、token 雜湊、只綁 127.0.0.1)仍是功能需求,照常實作與驗收。
- 需求分析紀錄:代問模式未採用——本環境的子 agent 無法直接與使用者對話,由同一 agent 提問並整理,確認權在使用者。
- 時間一律 UTC+0。

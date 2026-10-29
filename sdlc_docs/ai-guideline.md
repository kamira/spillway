# AI Guideline — Spillway

- 專案:spillway
- 分支(Branch):main
- 帳本落點(Ledger root):`sdlc_docs/` ← `docs/` 已存放專案自己的企劃書、規格書與 UI 設計稿,帳本不寫入該目錄
- 版本:v1.3
- 日期:2026-10-29(UTC+0)
- 狀態:已確認(2026-10-29,使用者)
- Skill:ai-sdlc v1.67

## 0. 需求來源與優先序

| 來源 | 內容 | 角色 |
|------|------|------|
| `docs/spec.md`(2026-10-29) | 資料模型、路由、額度、effort、adapter、MCP、HTTP API、UI 清單、NFR-1–7、AC-1–18 | **規範來源**:條文編號(如 4.2.5)為需求的正式依據 |
| `docs/proposal.md`(2026-10-29) | 背景、目標 G1–G7、範圍、選型、合規邊界、里程碑、風險 | 目的與範圍;與規格書衝突時以規格書為準(企劃書第三節自述) |
| `docs/ui/*.dc.html` | 7 個畫面的 layout | UI-1–UI-7 的版面依據;畫面數值為示意 |

本 Guideline 不複寫規格條文,只做索引、範圍界定、補足規格書未定的決策。規格書條文若要修改,走 modification-guide(CHG),並同步本文件升版。

## 1. 背景與目標

單人本機使用的 LLM 路由閘道。訂閱 CLI、雲端 API key、local 模型分散計費,沒有單一入口決定「這次用哪個模型」,導致訂閱附贈額度用不完時仍在付 API 費用。Spillway 對外只暴露 route 名稱,依使用者排定的 slot 順序(預設「訂閱 → API → local」)選有額度的成員,並顯示各成員剩餘額度。

成功定義 = 企劃書目標 G1–G7 全數由規格書第 12 節的驗收項(AC-1–18)通過證明。

## 2. 範圍

### 納入(依里程碑,出自企劃書第八節)

| 里程碑 | 內容 | 閘門 |
|--------|------|------|
| M1 規格定案 | 企劃書、規格書、UI layout | 規格審閱通過(**已完成**,文件已在 `docs/`) |
| M2 路由核心 | SQLite schema 與帳本、路由器與額度計算、LiteLLM adapter(api、local)、OpenAI 相容 HTTP API、管理 API | 路由測試全過(G3、G4) |
| M3 CLI 與 MCP | claude-cli、codex-cli adapter;MCP server(Streamable HTTP + stdio 橋接);MCP 設定產生器 | 4 種工具可連線(G1、G6) |
| M4 Web UI | 7 個畫面、設定匯入匯出、首次設定精靈 | 空 DB 首呼 ≤ 10 分(G2、G5、G7) |

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
| FR-20 | Web UI 7 個畫面(UI-1–UI-7)與拖曳規則 | 10 | M4 | P0 |
| FR-21 | YAML 設定匯入(merge/replace、單一交易)與匯出(不含憑證、不含 payload) | 3 | M4 | P0 |
| FR-22 | 首次設定精靈 | 10(UI-7) | M4 | P1 |
| FR-23 | 每個 adapter 模組可設定可用模型清單(`models.yaml`,local 可另查上游);能力預設跟著模型走 | 7.6.4;design D12 | M2 | P0 |
| FR-24 | 後台以連動選單選取 kind → provider → model,effort 選單只列模型支援的等級;資料來自 `GET /api/adapters`;保留「其他(手動輸入)」 | 9.2.2、10.2;design D13 | M2(端點)、M4(畫面) | P0 |
| FR-25 | 訂閱制 CLI 與 API 為不同模組;模組宣告 `cardinality`(single/multiple),建立成員與匯入時強制;內建模組 claude_cli、codex_cli(single)、anthropic_api、openai_api、litellm(multiple) | 2.1.4、7.6.5、7.6.9、9.2.3;design D14、D15 | M2(api 與 litellm)、M3(CLI) | P0 |

## 5. 非功能需求

以規格書第 11 節 NFR-1–7 為準,摘要:

| 類別 | 要求 | 依據 |
|------|------|------|
| 效能 | 路由決策 P95 < 20 ms;`/api/dashboard` P95 < 200 ms(100 成員、10,000 列帳本) | NFR-1、NFR-2 |
| 安全 | 預設只綁 127.0.0.1;資料庫、匯出檔、日誌無金鑰明文;`usage_ledger` 不存內容,payload 過期 1 小時內刪除 | NFR-3、NFR-4、NFR-5 |
| 並行 | CLI 成員同時執行數上限預設 1,超過不排隊 | NFR-6 |
| 相容性 | Python 3.11+;macOS、Linux、Windows | NFR-7(驗收只在 macOS,見第 6 節 Q2) |
| 可維護性 | 新增供應商只加 adapter 檔;新增 MCP 工具只加模板檔;CLI 參數、上游訊號規則放在成員設定 | 7.1.2、8.2.3、5.3.1、6.3.2 |

## 6. 限制與假設

- 限制:技術選型 Python + FastAPI + LiteLLM SDK + SQLite(WAL)(企劃書第六節)。前端為伺服端渲染加少量 JavaScript,不引入前端建置流程。
- 限制:尚未推送到 GitHub;目前無遠端、無 CI。
- 限制:威脅模型僅擁有者本人本機使用(knowledge DIR-2)。
- 假設:企劃書與規格書衝突時以規格書為準(例:負載平衡以 slot 為單位,而非企劃書寫的「訂閱層與 API 層內部」)。
- 假設:claude-cli 的 effort 參數名稱、CLI 輸出格式於實作當時以已安裝版本確認(spec 6.3.2、8.2.4)。
- 已確認(2026-10-29,使用者):
  - Q1 上游驗證策略:自動測試以**假上游**執行——假的 `claude`、`codex` 執行檔與假 API 回傳規格書定義的輸出、錯誤與訊號(5.3.1),可重複執行;額度用完、429、重置後恢復只在假上游驗證。每個里程碑閘門另由擁有者以真實帳號手動執行一次**真實冒煙**(含 AC-9 接 4 種工具),結果記入該 ACC。
  - Q2 平台驗收:驗收只在 macOS 執行;Linux、Windows 在 ACC 明標「未涵蓋」,待推到 GitHub 加 CI matrix 後補驗。程式仍依 NFR-7 寫成跨平台(路徑、子行程、keyring)。
- 待確認:無。

## 7. 驗收條件

以規格書第 12 節為準,依里程碑分組:

- [ ] M2 閘門:AC-3、AC-4、AC-5、AC-7、AC-13、AC-14、AC-15、AC-18、AC-19 通過;NFR-1、NFR-2、NFR-3、NFR-5 量測達標。4.2.5、4.2.6 的邏輯先以 api 成員驗證。
- [ ] M3 閘門:AC-1、AC-2、AC-9、AC-11、AC-12、AC-16、AC-17 通過(前置條件皆用到 claude-sub);NFR-6 量測達標。
- [ ] M4 閘門:AC-6、AC-8、AC-10、AC-20、AC-21 通過;NFR-4 對資料庫、匯出檔、日誌三者搜尋命中數為 0。
- [ ] 全部:每個閘門在 macOS 通過,並附擁有者的真實冒煙結果(Q1);Linux、Windows 標「未涵蓋」(Q2)。

## 7.1 變更歷程

| 版本 | 日期 | 內容 |
|------|------|------|
| v1.0 | 2026-10-29 | 使用者確認 |
| v1.1 | 2026-10-29 | 結構設計時使用者追加:FR-11 改為 adapter 模組化;新增 FR-23 可用模型清單、FR-24 後台選單 |
| v1.2 | 2026-10-29 | 使用者追加:訂閱 CLI 與 API 為不同模組;模組宣告唯一或多個(FR-25) |
| v1.3 | 2026-10-29 | CHG-20261029-01:FR-11、FR-23–25 已寫入規格書,改引條文編號;AC-19 列入 M2、AC-20–21 列入 M4 |

## 8. AI 開發約定

- 治理帳本:`sdlc_docs/`(changes/、acceptance/、structure/、knowledge/);入口為 root `AGENTS.md`。
- 需求依據:實作與驗收引用 `docs/spec.md` 的條文編號;規格書不在 CHG 外被修改。
- Adapter 模組化:新增一種 AI 只新增 `src/spillway/adapters/<module>/`,不改既有檔案(knowledge DIR-3)。
- 每個里程碑拆成一筆或多筆 CHG;每個 commit 訊息帶 CHG 編號。
- 審議機制:最簡化——審議席只給程式碼位置與意見,由獨立審議席統整權衡(knowledge DIR-1)。
- 安全審議:有限範圍,不考慮外部有權限者(knowledge DIR-2)。規格書內的安全功能(遮罩、token 雜湊、只綁 127.0.0.1)仍是功能需求,照常實作與驗收。
- 需求分析紀錄:代問模式未採用——本環境的子 agent 無法直接與使用者對話,由同一 agent 提問並整理,確認權在使用者。
- 時間一律 UTC+0。

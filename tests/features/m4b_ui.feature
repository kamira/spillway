# CHG-20261103-01 使用者故事 1、2、4、7、8、9(3、5、6 由 tests/ui 的 L2 涵蓋);M4 閘門 AC-6、AC-8、AC-10、AC-20、AC-21、AC-22;spec 10、10.1、10.2;NFR-4
# 步驟以 Playwright(Chromium,headless)操作行程內 live gateway(tests/helpers/live_gateway.start_live_gateway)的畫面;上游一律為本機假端點
Feature: M4b Web UI 畫面、首次設定精靈與 API 參考

  Background:
    Given 一個以假 API 上游啟動的閘道與瀏覽器

  Scenario: 總覽顯示每個成員的剩餘額度或冷卻至 (AC-6)
    Given 成員 "api-a" 額度為每月 10 美元且已花費 4 美元
    And 成員 "api-b" 額度為每月 10 美元且冷卻至 1 小時後
    When 開啟畫面 "總覽"
    Then 成員 "api-a" 的列顯示剩餘額度 "$6.00"
    And 成員 "api-b" 的列顯示冷卻至的時間
    When 在成員 "api-b" 的列按下 "清除冷卻"
    Then 成員 "api-b" 的列不再顯示冷卻

  Scenario: 空資料庫依首次設定精靈加入 1 個 API 成員並呼叫 (AC-8)
    Given 閘道行程的環境變數 "SPILLWAY_TEST_KEY" 已設定為假金鑰
    When 開啟畫面 "總覽"
    Then 畫面導向首次設定精靈
    When 在精靈中依序選類型 "api"、供應商 "openai"、第一個模型,上游網址填假上游,憑證參照填 "env:SPILLWAY_TEST_KEY"
    And 在精靈中按下 "測試呼叫"
    Then 精靈顯示測試結果為成功,且假上游收到該模型的請求
    When 在精靈中建立 route "chat" 並完成精靈
    Then 精靈顯示可複製的呼叫範例
    And 以該範例的 model 呼叫 "/v1/chat/completions" 回應狀態為 200
    And 從開啟精靈到呼叫成功的時間不超過 10 分鐘

  Scenario: 成員表單以連動選單選取類型、供應商、模型 (AC-20)
    When 開啟畫面 "成員" 並按下 "新增成員"
    And 選類型 "cli"
    Then 供應商選單只列出支援 "cli" 的模組
    When 選供應商 "claude-cli" 與第一個模型
    Then 模型選單的項目與 "claude_cli" 模組的 models.yaml 一致,最後一項為 "其他(手動輸入)"
    And 能力欄位帶入該模型的 capabilities

  Scenario: 無法載入的模組在供應商選單停用並顯示原因 (AC-21)
    Given 閘道以含一個 __init__.py 無法 import 的 adapter 模組的 extra_adapter_paths 啟動
    When 開啟畫面 "成員" 並按下 "新增成員"
    And 選該模組宣告的類型
    Then 該模組的供應商選項為停用並顯示原因

  Scenario: API 參考顯示四大類與可折疊索引,且與 mcp_catalog 一致 (AC-22)
    When 開啟畫面 "API 參考"
    Then 索引有 "連線"、"工具"、"通知"、"錯誤碼" 四大類
    And 收合 "工具" 之後其項目不再顯示,再展開後恢復
    And 畫面上的工具名稱、通知與錯誤碼與 mcp_catalog 的內容一致
    And 畫面不含 "/api/" 與 "/v1" 字樣

  Scenario: Route 編輯頁把 slot 拖到第一位並儲存後,請求改由該成員處理 (spec 10.1;AC-2 經畫面)
    Given task route "review" 的手動順序為 slot 1 "api-a"、slot 2 "api-b"
    When 開啟 route "review" 的編輯頁
    Then 編輯頁的 slot 依序為 "api-a"、"api-b"
    When 把 slot 2 整列拖到 slot 1 之前
    Then 畫面頂端標示有未儲存的變更
    When 按下 "儲存"
    Then route "review" 的手動順序 slot 1 為 "api-b"
    And 送出 3 次 task 請求皆由 "api-b" 處理

  Scenario: 匯入設定前顯示 dry_run 結果與 privileged 成員,確認後才寫入 (spec 3.1;CHG-20261102-02 決策 20)
    When 開啟畫面 "設定",選擇一份把成員 "api-b" 的 params.api_base 改掉的設定檔與 mode "replace",按下 "預覽匯入"
    Then 確認對話框列出計數與 privileged 成員 "api-b",資料尚未改變
    When 在確認對話框按下 "取消"
    Then 成員 "api-b" 的 params.api_base 不變,且沒有送出非 dry_run 的匯入請求
    When 再次按下 "預覽匯入" 並在確認對話框按下 "匯入"
    Then 畫面顯示備份檔名,成員 "api-b" 的 params.api_base 已改變

  Scenario: 畫面以純文字顯示使用者資料,不執行其中的標記 (UI 安全)
    Given 成員 "api-a" 的 tags 含 "<img src=x onerror=alert(1)>"
    And 帳本有一列的 error 為 "<script>alert(1)</script>"
    When 依序開啟畫面 "總覽"、"成員"、"帳本"
    Then 畫面上以文字顯示這兩個字串
    And 頁面沒有執行任何對話框,也沒有新增 img 或 script 元素

  Scenario: 畫面與頁面原始碼不含存取 token 與金鑰明文 (NFR-4 的畫面部分;資料庫、匯出檔、日誌由操作驗收檢查)
    Given 已產生過存取 token,且成員 "api-a" 的憑證參照指向值為假金鑰的環境變數
    When 依序開啟全部 8 個畫面
    Then 每個畫面的 HTML 與畫面呼叫的 API 回應都不含該 token 與假金鑰值
    When 在畫面 "設定" 按下 "重新產生 token" 並確認
    Then 對話框顯示新 token 一次;關閉對話框後頁面 DOM 與輸入欄位都不含新 token
    And 重新整理後畫面只顯示 "已產生"

  Scenario: 經畫面匯出設定,於另一個空資料目錄經確認對話框 replace 匯入後再匯出,兩份逐位元組相同 (AC-10)
    Given 已有 2 個成員、1 個群組與 1 條有手動順序的 route
    When 在畫面 "設定" 匯出設定為檔案 A
    And 在另一個空資料目錄的閘道開啟畫面 "設定",以 mode "replace" 預覽匯入檔案 A 並在確認對話框按下 "匯入"
    And 在該閘道的畫面 "設定" 匯出設定為檔案 B
    Then 檔案 A 與 B 逐位元組相同,且 A 不含假金鑰值

# CHG-20261030-02 使用者故事 4、5、6;spec 2.4;AC-13、AC-14、AC-15
Feature: Context 記錄、遮罩與保存期限

  Background:
    Given 一個空的閘道資料目錄與假 API 上游
    And 成員 "api-a" 類型 "api" 優先權重 "medium" 額度比例 1
    And target 只含所列成員的 route "review" 順序為 slot 1 "api-a"

  Scenario: 預設開啟時保存請求與回應 (AC-13、NFR-5)
    When 客戶端以 model "review" 送出 1 次請求
    Then usage_payloads 有 1 列
    And 該列含請求內容與回應內容
    And usage_ledger 沒有任何 prompt 或回應內容欄位

  Scenario: 過期內容被清除並回 410 (AC-14)
    Given context 保存天數為 7
    And 客戶端以 model "review" 送出 1 次請求
    When 系統時間推進 8 天並執行清理
    Then 該次的 usage_payloads 列已刪除
    And 查詢該次 payload 回應狀態為 410 且錯誤碼為 "payload_expired"

  Scenario: 金鑰被遮罩 (AC-15)
    When 客戶端以 model "review" 送出 prompt "請用 sk-test-0000 呼叫"
    Then 儲存的請求內容不含 "sk-test-0000"
    And 該列 redacted 為 1

  Scenario: route 關閉 context 記錄時不保存內容 (2.4.2)
    Given route "review" 的 record_context 為 0
    When 客戶端以 model "review" 送出 1 次請求
    Then usage_payloads 有 0 列
    And usage_ledger 有 1 列

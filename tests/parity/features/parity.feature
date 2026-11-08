# CHG-20261108-01 使用者故事 2、3;spec 15
Feature: 跨實作一致性比對與差距清單

  Scenario: 同一組情境送進兩個實作,正規化後沒有差異
    Given 第一實作與第二實作都是 Python 版
    When 以語料的每個情境分別操作兩個實作
    Then 每個情境宣告的比對面都有內容
    And 依 spec 15 比對的差異為 0 項

  Scenario: 第二實作改動線上內容時判定不一致
    Given 第一實作是 Python 版,第二實作是會改動回應 JSON body 的突變 wrapper
    When 以語料的每個情境分別操作兩個實作
    Then 比對結果不一致
    And 差異報告指出 JSON body 的欄位路徑
    And 差異報告不含金鑰或 token 值

  Scenario: 差距清單內的測試意外通過時判定失敗
    Given 差距清單列了一個實際會通過的測試
    When 以 SPILLWAY_IMPL=rust 套用差距清單執行該測試
    Then 該次執行判定失敗,並指出要刪除的清單項

  Scenario: 受保護的測試不得列入差距清單
    Given 差距清單列了一個受保護清單內的測試
    When 檢查差距清單檔案
    Then 檢查失敗,並指出該測試 id

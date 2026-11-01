# CHG-20261101-02 使用者故事 1、2、3;決策 1、4、12
Feature: M2 整理後的憑證鍵名阻擋與健康快取失效

  Scenario: 成員 params 含變體憑證鍵名時被拒且不回顯值
    Given 一個空的閘道資料目錄與假 API 上游
    When 以管理 API 建立 api 成員 "api-x",params 為 {"x-api-key": "plain-value-123"}
    Then 管理 API 回應狀態為 422 且錯誤碼為 "validation_error"
    And 錯誤訊息含 "x-api-key" 且不含 "plain-value-123"
    And 資料庫中沒有成員 "api-x"

  Scenario: params.signals 的合法覆寫規則不被鍵名比對誤擋
    Given 一個空的閘道資料目錄與假 API 上游
    When 以管理 API 建立 api 成員 "api-s",params.signals 覆寫 "rate_limit" 規則為 HTTP 狀態 429
    Then 管理 API 回應狀態為 201
    And 成員 "api-s" 的 params.signals 與送出的內容相同

  Scenario: 修改 local 成員的 api_base 後立即以新設定判斷健康
    Given 一個空的閘道資料目錄與假 API 上游
    And local 成員 "loc-1",provider "hosted_vllm",api_base 指向一個沒有服務的埠
    When 客戶端以 model "local/loc-1" 送出 1 次請求
    Then 回應狀態為 503
    When 以管理 API 把成員 "loc-1" 的 api_base 改為指向假 API 上游
    And 客戶端以 model "local/loc-1" 送出 1 次請求,系統時間不前進
    Then 回應狀態為 200
    And 回應 header "X-Spillway-Served-By" 為 "loc-1"

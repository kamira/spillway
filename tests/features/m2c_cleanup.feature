# CHG-20261101-02 使用者故事 1、2、3;決策 1、4、12、17
Feature: M2 整理後的憑證鍵名阻擋、signals 寫入驗證、健康快取失效與無 seed 加權抽選

  Scenario: 成員 params 含變體憑證鍵名時被拒且不回顯值
    Given 一個空的閘道資料目錄與假 API 上游
    When 以管理 API 建立 api 成員 "api-x",params 為 {"x-api-key": "plain-value-123"}
    Then 管理 API 回應狀態為 422 且錯誤碼為 "validation_error"
    And 錯誤訊息含 "x-api-key" 且不含 "plain-value-123"
    And 資料庫中沒有成員 "api-x"

  Scenario: params.signals 的合法結構可寫入、不合法格式寫入時即被拒
    Given 一個空的閘道資料目錄與假 API 上游
    When 以管理 API 建立 api 成員 "api-s",params.signals 覆寫 "rate_limit" 規則為 HTTP 狀態 429
    Then 管理 API 回應狀態為 201
    And 成員 "api-s" 的 params.signals 與送出的內容相同
    When 以管理 API 建立 api 成員 "api-t",params.signals 含未知類別鍵 "no_such_kind"
    Then 管理 API 回應狀態為 422 且錯誤碼為 "validation_error"
    And 資料庫中沒有成員 "api-t"

  Scenario: 修改 local 成員的 api_base 後,經 route 的下一次請求立即以新設定判斷健康
    Given 一個空的閘道資料目錄與假 API 上游
    And local 成員 "loc-1",provider "hosted_vllm",api_base 指向一個沒有服務的埠
    And target 只含所列成員的 route "lr" 順序為 slot 1 "loc-1"
    When 客戶端以 model "lr" 送出 1 次請求
    Then 回應狀態為 503
    When 以管理 API 把成員 "loc-1" 的 api_base 改為指向假 API 上游
    And 客戶端以 model "lr" 送出 1 次請求,系統時間不前進
    Then 回應狀態為 200
    And 回應 header "X-Spillway-Served-By" 為 "loc-1"

  Scenario: 未給 seed 的亂數來源下,同 slot 權重 high 與 low 的第一順位比例
    Given 一個空的閘道資料目錄與假 API 上游
    And 成員 "api-a" 類型 "api" 優先權重 "high" 額度比例 1
    And 成員 "api-b" 類型 "api" 優先權重 "low" 額度比例 1
    And 加權抽選使用未給 seed 的亂數來源
    And target 只含所列成員的 route "review" 順序為 slot 1 "api-a,api-b"
    When 對 route "review" 執行 4000 次 dry-run
    Then "api-a" 被選為第一順位的比例介於 0.70 與 0.80

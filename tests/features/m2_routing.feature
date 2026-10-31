# CHG-20261030-02 使用者故事 1、2、3、7;spec 4.1.1、4.3、4.4、4.5;AC-3、AC-4、AC-5
Feature: 路由與 fallback

  Background:
    Given 一個空的閘道資料目錄與假 API 上游
    And 成員 "api-a" 類型 "api" 優先權重 "high" 額度比例 1
    And 成員 "api-b" 類型 "api" 優先權重 "low" 額度比例 1
    And 成員 "api-c" 類型 "api" 優先權重 "medium" 額度比例 1
    And local 成員 "qwen-local",provider "hosted_vllm",api_base 指向假 API 上游
    And 加權抽選使用固定 seed 的亂數來源

  Scenario: slot 1 冷卻時由 slot 2 接手 (AC-3)
    Given target 只含所列成員的 route "review" 順序為 slot 1 "api-a,api-b"、slot 2 "api-c"
    And 成員 "api-a" 與 "api-b" 在冷卻中
    When 客戶端以 model "review" 送出 1 次請求
    Then 回應狀態為 200
    And 回應 header "X-Spillway-Served-By" 為 "api-c"
    And 回應 header "X-Spillway-Slot" 為 "2"
    And 假上游總共收到 1 次請求

  Scenario: slot 內依優先權重分攤 (AC-4)
    Given target 只含所列成員的 route "review" 順序為 slot 1 "api-a,api-b"
    When 對 route "review" 執行 400 次 dry-run
    Then "api-a" 被選為第一順位的比例介於 0.70 與 0.80

  Scenario: local/ 直呼不經路由 (AC-5)
    When 客戶端以 model "local/qwen-local" 送出 1 次請求
    Then 回應 header "X-Spillway-Served-By" 為 "qwen-local"
    And 該次帳本列的 slot 為 NULL

  Scenario: 找不到 route
    When 客戶端以 model "no-such-route" 送出 1 次請求
    Then 回應狀態為 404
    And 錯誤碼為 "route_not_found"

  Scenario: 全部失敗回 503 並列出每次嘗試
    Given target 只含所列成員的 route "review" 順序為 slot 1 "api-a"、slot 2 "api-c"
    And 假上游對所有請求回傳 HTTP 500
    When 客戶端以 model "review" 送出 1 次請求
    Then 回應狀態為 503
    And 錯誤碼為 "no_candidate"
    And 錯誤內容依序列出 "api-a:error,api-c:error"

  Scenario: 限流後冷卻並在到期後自動恢復 (4.2.6、4.5.2)
    Given target 只含所列成員的 route "review" 順序為 slot 1 "api-a"、slot 2 "api-c"
    And 假上游對 "api-a" 回傳 HTTP 429
    When 客戶端以 model "review" 送出 1 次請求
    Then 回應 header "X-Spillway-Served-By" 為 "api-c"
    And 成員 "api-a" 冷卻至現在加 60 秒
    When 假上游恢復正常且系統時間推進 61 秒
    And 客戶端以 model "review" 送出 1 次請求
    Then 回應 header "X-Spillway-Served-By" 為 "api-a"

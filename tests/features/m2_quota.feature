# CHG-20261030-02 使用者故事 10;spec 4.2.5、4.2.6、5.1、5.3.2;Guideline 第 7 節「4.2.5、4.2.6 的邏輯先以 api 成員驗證」
Feature: api 成員的預估檢查與額度冷卻

  Background:
    Given 一個空的閘道資料目錄與假 API 上游
    And 成員 "api-a" 類型 "api" 優先權重 "medium" 額度比例 1
    And 成員 "api-c" 類型 "api" 優先權重 "medium" 額度比例 1

  Scenario: 預估超過剩餘時跳過並由下一位處理 (AC-16 api 版,4.2.5)
    Given target 只含所列成員的 route "review" 順序為 slot 1 "api-a"、slot 2 "api-c"
    And route "review" 勾選預估額度檢查
    And 成員 "api-a" 剩餘額度為 0.01 美元且本次請求預估 P90 花費為 0.05 美元
    When 客戶端以 model "review" 送出 1 次請求
    Then 回應 header "X-Spillway-Served-By" 為 "api-c"
    And 成員 "api-a" 的帳本列狀態為 "skipped_precheck"
    And 成員 "api-a" 不在冷卻中

  Scenario: 上游回報額度用完後冷卻到重置點並自動恢復 (AC-17 api 版,4.2.6)
    Given target 只含所列成員的 route "review" 順序為 slot 1 "api-a"、slot 2 "api-c"
    And 成員 "api-a" 的額度為每月重置、重置日為 1 日
    And 系統時間為 "2026-10-30T12:00:00Z"
    And 假上游對 "api-a" 回傳 HTTP 402
    When 客戶端以 model "review" 送出 1 次請求
    Then 回應 header "X-Spillway-Served-By" 為 "api-c"
    And 成員 "api-a" 冷卻至 "2026-11-01T00:00:00Z"
    When 假上游恢復正常且系統時間推進至 "2026-11-01T00:00:01Z"
    And 客戶端以 model "review" 送出 1 次請求
    Then 回應 header "X-Spillway-Served-By" 為 "api-a"

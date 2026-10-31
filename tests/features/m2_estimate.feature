# CHG-20261030-02 使用者故事 7;spec 5.4;AC-18
Feature: dry-run 的預估值

  Background:
    Given 一個空的閘道資料目錄與假 API 上游
    And 成員 "api-a" 類型 "api" 優先權重 "medium" 額度比例 1
    And target 只含所列成員的 route "review" 順序為 slot 1 "api-a"

  Scenario: dry-run 顯示的 P50、P90 與帳本一致 (AC-18)
    Given route "review" 與成員 "api-a" 已有 50 筆 ok 帳本紀錄
    When 對 route "review" 執行 1 次 dry-run
    Then 成員 "api-a" 的預估 P50 與 P90 等於由帳本最近 50 筆 ok 的 tokens_out 計算的值
    And 帳本列數與 dry-run 之前相同

  Scenario: 帳本不足 5 筆時使用預設值 (5.4.3)
    When 對 route "review" 執行 1 次 dry-run
    Then 成員 "api-a" 的預估 P50 為 500 且 P90 為 2000

  Scenario: 請求帶 max_tokens 時以它為預估 (5.4.3)
    When 對 route "review" 以 max_tokens 300 執行 1 次 dry-run
    Then 成員 "api-a" 的預估 P50 為 300 且 P90 為 300

# CHG-20261030-02 使用者故事 9、12;spec 7.1.2、7.6;AC-19
Feature: Adapter 模組自動發現與 cardinality

  Scenario: 新增模組資料夾後被列出並可被路由 (AC-19)
    Given 一個空的閘道資料目錄與假 API 上游
    And 另外放入一個宣告 provider "acme" 的測試模組資料夾,未修改任何既有檔案
    When 啟動閘道
    Then GET /api/adapters 列出 provider "acme"
    And 合約測試對該模組通過
    When 以 provider "acme" 建立成員 "acme-1" 並建立 target 只含它的 route "acme-route"
    And 客戶端以 model "acme-route" 送出 1 次請求
    Then 回應 header "X-Spillway-Served-By" 為 "acme-1"

  Scenario: single 模組只允許 1 個成員 (7.6.5、9.2.3;AC-12 的 M2 邏輯部分,不計入 M2 閘門)
    Given 一個空的閘道資料目錄與假 API 上游
    And 另外放入一個宣告 provider "solo" 且 cardinality 為 single 的測試模組資料夾
    When 啟動閘道
    And 以 provider "solo" 建立成員 "solo-1"
    And 以 provider "solo" 建立成員 "solo-2"
    Then 第二次建立回應狀態為 409 且錯誤碼為 "provider_single"
    And 資料庫中 provider "solo" 只有 1 個成員

  Scenario: 無法載入的模組不影響其他模組 (7.6.7;AC-21 的 M2 邏輯部分,不計入 M2 閘門)
    Given 一個空的閘道資料目錄與假 API 上游
    And 另外放入一個 __init__.py 無法 import 的測試模組資料夾,宣告 provider "broken"
    When 啟動閘道
    Then 閘道正常啟動
    And GET /api/adapters 顯示 provider "broken" 不可用並附原因
    And 使用 provider "broken" 的成員於候選篩選的原因為 "adapter_unavailable"

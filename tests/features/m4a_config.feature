# CHG-20261102-02 使用者故事 1–6;AC-10;NFR-4(匯出檔);spec 3、9.2;決策 20(使用者決定 U1:dry_run 與 privileged)
Feature: M4a 設定匯入匯出、帳本 CSV、設定總覽與群組名稱規則

  Background:
    Given 一個空的閘道資料目錄與假 API 上游
    And 成員 "api-a" 類型 "api" 優先權重 "high" 額度比例 1
    And 成員 "api-b" 類型 "api" 優先權重 "low" 額度比例 1
    And 成員 "api-a" 的 credential_ref 為 "env:SPILLWAY_TEST_KEY" 且該環境變數為測試假值
    And 群組 "coding" 的選擇器含 "api-a"、"api-b"
    And task route "review" 的 target 為群組 "coding",順序為 slot 1 "api-b"、slot 2 "api-a"

  Scenario: 匯出檔含成員、群組、route 與拖曳順序且不含憑證值 (spec 3.1.1;AC-10;NFR-4)
    When 以管理 API 匯出設定
    Then 回應為 YAML 且 version 為 1
    And 匯出檔含成員 "api-a"、"api-b",群組 "coding",route "review"
    And route "review" 的 targets 依序為 slot 1 "api-b"、slot 2 "api-a"
    And 成員 "api-a" 的 credential_ref 為 "env:SPILLWAY_TEST_KEY"
    And 匯出檔不含該環境變數的值,也不含存取 token 明文

  Scenario: 匯出後在空資料庫以 replace 匯入,再匯出內容相同 (AC-10)
    Given 已匯出目前設定為檔案 A
    When 在另一個空的閘道資料目錄以 mode "replace" 匯入檔案 A
    Then 匯入回應狀態為 200
    And 再次匯出的內容與檔案 A 相同
    And dry-run route "review" 的第一順位為 "api-b"

  Scenario: merge 匯入只更新檔案列出的項目 (spec 3.1.3)
    When 以 mode "merge" 匯入只含成員 "api-b" 且其 tags 為 ["coding", "cheap"] 的設定
    Then 匯入回應狀態為 200
    And 成員 "api-b" 的 tags 為 ["coding", "cheap"]
    And 成員 "api-a" 與 route "review" 不變

  Scenario: 任一欄位驗證失敗時整批不寫入並回傳欄位路徑 (spec 3.1.2)
    When 以 mode "replace" 匯入第二個成員的 kind 為 "bogus" 的設定
    Then 管理 API 回應狀態為 422 且錯誤碼為 "validation_error"
    And 錯誤訊息含欄位路徑 "deployments[1].kind"
    And 成員、群組與 route 皆與匯入前相同

  Scenario: replace 匯入前自動備份目前設定
    When 以 mode "replace" 匯入只含成員 "api-c" 的設定
    Then 匯入回應狀態為 200
    And 資料目錄的 backups 下有一個備份檔,內容等於匯入前的匯出
    And 帳本列數與匯入前相同

  Scenario: dry_run 匯入不寫入並列出變更 privileged 欄位的成員 (spec 3.1;決策 20)
    When 以 mode "replace" 且 dry_run 匯入把成員 "api-b" 的 params.api_base 改為 "http://127.0.0.1:9/v1" 的設定
    Then 匯入回應狀態為 200
    And 匯入回應的 dry_run 為 true 且 backup 為空
    And 匯入回應的 privileged 為 ["api-b"]
    And 成員、群組與 route 皆與匯入前相同
    And 資料目錄的 backups 下沒有備份檔

  Scenario: 匯入違反 single 模組限制時回 409 (spec 9.2.3)
    When 以 mode "merge" 匯入兩個 provider 為 "claude-cli" 的成員
    Then 管理 API 回應狀態為 409 且錯誤碼為 "provider_single"
    And 成員、群組與 route 皆與匯入前相同

  Scenario: 帳本匯出 CSV 依篩選條件且不含 payload (UI-6)
    Given 帳本有 3 列,其中 2 列屬於 route "review"
    When 以管理 API 匯出 route 為 "review" 的帳本 CSV
    Then CSV 有標題列與 2 列資料
    And CSV 不含任何請求或回應內容

  Scenario: 設定總覽不含 token 明文 (UI-7、首次設定精靈)
    When 以管理 API 取得設定總覽
    Then 總覽含 port、data_dir、token_issued、first_run 與 counts
    And 總覽不含存取 token 明文

  Scenario: 新群組名稱不可為 "." 或 ".." 或含控制字元 (CHG-20261102-01 已知限制)
    When 以管理 API 的請求 body 建立名為 ".." 的群組
    Then 管理 API 回應狀態為 422 且錯誤碼為 "validation_error"
    When 以管理 API 的請求 body 建立名稱含 U+202E 的群組
    Then 管理 API 回應狀態為 422 且錯誤碼為 "validation_error"

# CHG-20261102-01 使用者故事 1、2(list_routes)、3–7;AC-11(新文字);spec 8.1、8.2、9.1.4;決策 14、18
# quota_status 由 T2 整合測試涵蓋;故事 8(UI-8)本筆只登記需求,不在本檔
# 需要 GET SSE、stdio 或停機的情境以 live_gateway 或子行程 serve 執行(ASGITransport 會緩衝回應);其餘 HTTP 層情境可用 ASGITransport
Feature: M3b MCP server、mcp-stdio、設定輸出與 Host/Origin 檢查

  Background:
    Given 一個空的閘道資料目錄與假 API 上游
    And 假 CLI 執行檔在 PATH 最前面且 PATH 上沒有真實的 claude 與 codex
    And 已產生存取 token
    And CLI 成員 "claude-sub",provider "claude-cli",額度類型 none
    And 成員 "deepseek-api" 類型 "api" 優先權重 "medium" 額度比例 1
    And 群組 "coding" 的選擇器含 "claude-sub"、"deepseek-api"
    And task route "review" 的 target 為群組 "coding",順序為 slot 1 "claude-sub"、slot 2 "deepseek-api"
    And route "chat-x" 的 target 含 "deepseek-api",採預設順序

  Scenario: 群組端點只列出該群組的 route,all 列出全部 (spec 8.1.4)
    When MCP 用戶端以正確 token 連線 "/mcp/coding" 並列出工具
    Then 工具 "ask" 的 route 選項只有 "review"
    When MCP 用戶端以正確 token 連線 "/mcp/all" 並列出工具
    Then 工具 "ask" 的 route 選項為 "chat-x"、"review"

  Scenario: ask 由 route 處理並回報成員、slot 與花費 (spec 8.1、4.2.3)
    When MCP 用戶端以正確 token 連線 "/mcp/coding" 並以 route "review" 呼叫 ask
    Then ask 結果不是錯誤且 served_by 為 "claude-sub"、slot 為 1
    And ask 結果帶 cost_usd 欄位
    And 帳本最新一列的 route_name 為 "review"

  Scenario: list_routes 的順序與管理 API 一致 (spec 8.1、4.3;使用者故事 2)
    When MCP 用戶端以正確 token 連線 "/mcp/coding" 並呼叫 list_routes
    Then list_routes 結果只含 route "review"
    And route "review" 的 slot 與成員順序與 "GET /api/routes/review" 的 order 相同

  Scenario: 沒有或錯誤的 token 被拒,rotate 後舊 token 被拒 (spec 8.1.3)
    When 以不帶 Authorization 的請求送到 "/mcp/coding"
    Then HTTP 回應狀態為 401 且錯誤碼為 "unauthorized"
    When 以錯誤的 token 送到 "/mcp/coding"
    Then HTTP 回應狀態為 401 且錯誤碼為 "unauthorized"
    When 以管理 API rotate 存取 token
    And 以 rotate 前的 token 送到 "/mcp/coding"
    Then HTTP 回應狀態為 401 且錯誤碼為 "unauthorized"

  # 後半以子行程 serve + 子行程 mcp-stdio 執行
  Scenario: 遞迴呼叫被拒 (spec 8.1.6;AC-11)
    When 以正確 token 且 header "X-Spillway-Depth" 為 "1" 送到 "/mcp/coding"
    Then HTTP 回應狀態為 403 且錯誤碼為 "recursion_blocked"
    When 以環境變數 SPILLWAY_DEPTH 為 "1" 執行 mcp-stdio --group coding,stdio 用戶端送出 initialize
    Then stdio 用戶端收到 JSON-RPC 錯誤且 error.data.code 為 "recursion_blocked"
    And mcp-stdio 的結束碼為 3

  # 以 live_gateway 執行(需要真實 GET SSE 串流)
  Scenario: 新增 route 後已連線的 session 收到 list_changed (spec 8.1.5)
    Given MCP 用戶端以正確 token 連線 "/mcp/coding" 並開啟通知串流
    When 以管理 API 建立 task route "review2",target 為群組 "coding"
    Then MCP 用戶端在 5 秒內收到 "notifications/tools/list_changed"
    And 重新列出工具後工具 "ask" 的 route 選項為 "review"、"review2"

  Scenario: 跨站 Origin 與非本機 Host 的請求被拒 (spec 9.1.4)
    Given 記錄目前存取 token 的雜湊
    When 以 header "Origin" 為 "http://evil.example" 送 POST 到 "/api/settings/token/rotate"
    Then HTTP 回應狀態為 403 且錯誤碼為 "origin_not_allowed"
    And 存取 token 的雜湊未改變
    When 以 header "Host" 為 "evil.example:8787" 送 GET 到 "/v1/models"
    Then HTTP 回應狀態為 403 且錯誤碼為 "host_not_allowed"

  Scenario: 設定輸出 5 種工具皆以 SPILLWAY_TOKEN 參照 token (spec 8.2)
    When 以管理 API 取得群組 "coding" 的 MCP 設定,工具依序為 "claude-code"、"codex"、"cursor"、"gemini"、"stdio"
    Then 每份設定的 env 為 ["SPILLWAY_TOKEN"] 且 content 含 "SPILLWAY_TOKEN"
    And 每份設定的 content 不含存取 token 明文
    And 除 "stdio" 外每份設定的 content 含 "http://127.0.0.1:8787/mcp/coding"

  # 以子行程 serve 或 live_gateway 執行(需要真實訊號與寬限期;ASGITransport 不適用)
  Scenario: 停機中進行中的 ask 得到 shutting_down (決策 14、18;ACC-20261101-02 F2)
    Given 閘道以子行程 serve 啟動且寬限期為 2 秒
    And 假 claude 收到 prompt 後持續執行不結束
    When MCP 用戶端以正確 token 連線 "/mcp/coding" 並在背景以 route "review" 呼叫 ask
    And 假 claude 已啟動後對 serve 送出 SIGTERM
    Then ask 結果為錯誤且 structuredContent 的錯誤碼為 "shutting_down"
    And 帳本最新一列的 error 為 "shutdown"
    And serve 在寬限期內結束且沒有殘留的假 claude 行程

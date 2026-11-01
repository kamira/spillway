# CHG-20261101-03 使用者故事 1–6;AC-1、AC-2、AC-12、AC-16(CLI 版)、AC-17(CLI 版)、NFR-6;spec 4.2.3、5.2.3、7.3、7.4
Feature: M3a CLI 成員(claude-cli、codex-cli)

  Background:
    Given 一個空的閘道資料目錄與假 API 上游
    And 假 CLI 執行檔在 PATH 最前面且 PATH 上沒有真實的 claude 與 codex
    And CLI 成員 "claude-sub",provider "claude-cli",額度 credit_pool 10 美元每月重置
    And CLI 成員 "codex-sub",provider "codex-cli",額度類型 none
    And 成員 "deepseek-api" 類型 "api" 優先權重 "medium" 額度比例 1

  Scenario: 預設順序下 task 請求由有額度的訂閱 CLI 成員處理 (AC-1)
    Given task route "review" 的 target 含 "claude-sub"、"deepseek-api",採預設順序
    When 客戶端以 model "review" 送出 10 次 task 請求
    Then 10 次回應的 header "X-Spillway-Served-By" 皆為 "claude-sub"

  Scenario: 擁有者把 api 成員拖到 slot 1 後由它處理 (AC-2)
    Given task route "review" 的 target 含 "claude-sub"、"deepseek-api",順序為 slot 1 "deepseek-api"、slot 2 "claude-sub"
    When 客戶端以 model "review" 送出 10 次 task 請求
    Then 10 次回應的 header "X-Spillway-Served-By" 皆為 "deepseek-api"

  Scenario: 第二個 claude-cli 成員被拒 (AC-12)
    When 以管理 API 建立 CLI 成員 "claude-sub-2",provider "claude-cli"
    Then 管理 API 回應狀態為 409 且錯誤碼為 "provider_single"

  Scenario: 預估花費超過 credit_pool 剩餘時跳過 CLI 成員 (AC-16 CLI 版)
    Given task route "review" 的 target 含 "claude-sub"、"deepseek-api",順序為 slot 1 "claude-sub"、slot 2 "deepseek-api"
    And route "review" 勾選預估額度檢查
    And CLI 成員 "claude-sub" 的 credit_pool 剩餘 0.01 美元且本次請求預估 P90 花費為 0.05 美元
    When 客戶端以 model "review" 送出 1 次 task 請求
    Then 回應 header "X-Spillway-Served-By" 為 "deepseek-api"
    And 成員 "claude-sub" 的帳本列狀態為 "skipped_precheck"

  Scenario: 上游回報額度用完後冷卻、重置後恢復 (AC-17 CLI 版)
    Given task route "review" 的 target 含 "claude-sub"、"deepseek-api",順序為 slot 1 "claude-sub"、slot 2 "deepseek-api"
    And 系統時間為 "2026-11-10T12:00:00Z"
    And 假 claude 的情境設為額度用完
    When 客戶端以 model "review" 送出 1 次 task 請求
    Then 回應 header "X-Spillway-Served-By" 為 "deepseek-api"
    And CLI 成員 "claude-sub" 冷卻至 "2026-12-01T00:00:00Z"
    When 假 claude 的情境設為成功且系統時間推進至 "2026-12-01T00:00:01Z"
    And 客戶端以 model "review" 送出 1 次 task 請求
    Then 回應 header "X-Spillway-Served-By" 為 "claude-sub"

  Scenario: CLI 成員併發額滿時由下一位接手、不排隊 (NFR-6)
    Given task route "review" 的 target 含 "claude-sub"、"deepseek-api",順序為 slot 1 "claude-sub"、slot 2 "deepseek-api"
    And 成員 "claude-sub" 的 max_concurrency 為 1,假 claude 會等到放行訊號才回應
    When 客戶端以 model "review" 送出第 1 次 task 請求並等到假 claude 已開始
    And 客戶端以 model "review" 送出第 2 次 task 請求
    And 放行假 claude
    Then 第 1 次由 "claude-sub" 處理、第 2 次由 "deepseek-api" 處理
    And 帳本中 "claude-sub" 只有 1 列

  Scenario: codex 成員花費記為 NULL 且依上游訊號判定狀態 (spec 5.2.3、5.3.1)
    Given task route "review-codex" 的 target 含 "codex-sub"、"deepseek-api",順序為 slot 1 "codex-sub"、slot 2 "deepseek-api"
    When 客戶端以 model "review-codex" 送出 1 次 task 請求
    Then 回應 header "X-Spillway-Served-By" 為 "codex-sub"
    And 成員 "codex-sub" 最新一列帳本的 cost_usd 為 NULL 且 tokens_out 有值
    When 假 codex 的情境設為限流
    And 客戶端以 model "review-codex" 送出 1 次 task 請求
    Then 回應 header "X-Spillway-Served-By" 為 "deepseek-api"
    And 成員 "codex-sub" 的帳本列狀態為 "rate_limited"

  Scenario: CLI 子行程不帶 API 金鑰與改道變數、帶深度標記、不載入任何 MCP (spec 7.3.2、7.3.3、7.4.3)
    Given 環境變數 "ANTHROPIC_API_KEY"、"ANTHROPIC_BASE_URL"、"OPENAI_API_KEY"、"OPENAI_BASE_URL" 設為測試假值
    And CLI 成員 "claude-sub" 的 params.extra_args 設為 ["--verbose"]
    And task route "review" 的 target 只含 "claude-sub"
    And task route "review-codex" 的 target 只含 "codex-sub"
    When 客戶端以 model "review" 送出 1 次 task 請求
    And 客戶端以 model "review-codex" 送出 1 次 task 請求
    Then 假 claude 收到的環境沒有 "ANTHROPIC_API_KEY" 與 "ANTHROPIC_BASE_URL",且 "SPILLWAY_DEPTH" 為 "1"
    And 假 claude 收到的參數含 "--strict-mcp-config" 與 "--verbose",prompt 由 stdin 傳入
    And 假 codex 收到的環境沒有 "OPENAI_API_KEY" 與 "OPENAI_BASE_URL",且 "SPILLWAY_DEPTH" 為 "1"
    And 假 codex 收到的參數含 "-c" 與 "mcp_servers={}",倒數第二個參數為 "--"

  Scenario: chat route 不選用 CLI 成員 (spec 4.2.3)
    Given route "chat-route" 的 target 含 "claude-sub"、"deepseek-api",採預設順序
    When 客戶端以 model "chat-route" 送出 1 次請求
    Then 回應 header "X-Spillway-Served-By" 為 "deepseek-api"

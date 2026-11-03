// 畫面字串對照表(CHG-20261103-01 決策 20;AM-17)。畫面文字為繁體中文。
// 代碼對應的標籤以 label(table, value) 取得;原值由呼叫端放在 title(見 ui.js 的 codeLabel)。

// 導覽 8 項的正式清單:各 HTML 的 <nav> 與 tests/helpers/ui.py 的 NAV 都對照這份(決策 20)
export const NAV = Object.freeze([
  ["總覽", "/ui/"],
  ["成員", "/ui/members.html"],
  ["群組與 Tag", "/ui/groups.html"],
  ["Route", "/ui/routes.html"],
  ["MCP 設定", "/ui/mcp.html"],
  ["帳本", "/ui/ledger.html"],
  ["設定", "/ui/settings.html"],
  ["API 參考", "/ui/api-reference.html"],
]);

// 成員類型(kind)
export const KIND = Object.freeze({ cli: "訂閱 CLI", api: "API", local: "Local" });

// 帳本與呼叫結果(status)
export const STATUS = Object.freeze({
  ok: "成功",
  error: "錯誤",
  quota_exhausted: "額度用完",
  rate_limited: "限流",
  skipped_precheck: "預檢略過",
});

// 冷卻原因(cooling_reason)
export const COOLING_REASON = Object.freeze({
  quota_exhausted: "額度用完",
  rate_limited: "限流",
});

// 額度類型(budget.type)
export const BUDGET_TYPE = Object.freeze({ usd: "USD", credit_pool: "點數池(USD)", none: "不設額度" });

// 額度週期(budget.window)
export const WINDOW = Object.freeze({ monthly: "每月", weekly: "每週", daily: "每日", none: "不重置" });

export const TEXT = Object.freeze({
  network_error: "無法連線到 spillway",
  http_error: "伺服器回應錯誤",
  bad_response: "伺服器回應的內容無法解讀",
  loading: "載入中…",
  cancel: "取消",
  confirm: "確定",
  close: "關閉",
  rotate_token: "重新產生 token",
  unlimited: "不限",
  cooling_until: "冷卻至",
});

export function label(table, value) {
  if (value === null || value === undefined) return "";
  return Object.prototype.hasOwnProperty.call(table, value) ? table[value] : String(value);
}

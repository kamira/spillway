// UI-8 API 參考(CHG-20261103-01 T7;決策 4、19,決策審議修訂 AM-17)。
// - 資料取自 GET /api/mcp/catalog?group=(預設 all);群組選單列出 all 與各群組。
// - 左側索引:四大類(連線、工具、通知、錯誤碼)以 <details>/<summary> 折疊(預設展開);
//   每個項目是 <a href="#<id>">,點選後網址 hash 更新,右側顯示該項目(可直接連結)。
// - 右側:連線(端點 URL 與各群組 URL、傳輸、驗證方式與環境變數名、mcp-stdio 指令)、工具(說明、
//   inputSchema、outputSchema 以縮排 JSON 顯示)、通知、錯誤碼(HTTP、工具、SDK 傳輸三組)。
// - 只呈現 MCP 介面:畫面文字不含管理介面與 OpenAI 相容端點的路徑。
// - 網址 ?group=<g> 決定初始群組;切換群組以 history.replaceState 更新 query(保留 hash)。只做 GET。

import { getJSON, withQuery } from "./api.js";
import { byId, clear, h } from "./dom.js";
import { clearMessage, fillGroupSelect, initShell, latest, setLoading, showError } from "./ui.js";

const T = Object.freeze({
  cat_connection: "連線",
  cat_tools: "工具",
  cat_notifications: "通知",
  cat_errors: "錯誤碼",
  conn_endpoint: "端點 URL",
  conn_auth: "驗證方式",
  conn_stdio: "mcp-stdio",
  err_http: "HTTP",
  err_tool: "工具",
  err_sdk: "SDK 傳輸",
  transport: "傳輸",
  this_group: "本群組端點",
  group_urls: "各群組端點",
  group: "群組",
  url: "URL",
  auth_type: "類型",
  auth_header: "標頭",
  auth_value: "值",
  auth_env: "環境變數",
  auth_note: "token 只以環境變數參照;於「設定」頁產生。",
  command: "指令",
  env: "環境變數",
  description: "說明",
  title: "標題",
  input_schema: "inputSchema",
  output_schema: "outputSchema",
  annotations: "annotations",
  delivery: "送達條件",
  code: "code",
  status: "HTTP 狀態",
  format: "格式",
  message: "訊息",
  no_tools: "此群組目前沒有可用的工具",
  not_found_item: "找不到這個項目,已改為顯示第一項。",
  http_errors_note: "閘道對 MCP 端點的請求以 spec 9.1.2 的 JSON 格式回應下列錯誤。",
  tool_errors_note: "工具呼叫失敗時回 CallToolResult(isError),code 在 structuredContent.error.code。",
  sdk_errors_note: "由 MCP SDK 產生(JSON-RPC 或純文字),不是 spec 9.1.2 格式。",
});

const state = { group: "all", catalog: null, items: new Map() };
const catalogGuard = latest();

function pretty(value) {
  return JSON.stringify(value, null, 2);
}

function dl(pairs) {
  const out = h("dl", {});
  for (const [k, v] of pairs) {
    if (v === undefined || v === null || v === "") continue;
    out.append(h("dt", {}, k), v instanceof Node ? h("dd", {}, v) : h("dd", {}, String(v)));
  }
  return out;
}

function mono(text) {
  return h("span", { class: "mono" }, String(text));
}

function table(headers, rows) {
  return h("div", { class: "scroll" },
    h("table", {},
      h("thead", {}, h("tr", {}, ...headers.map((x) => h("th", { scope: "col" }, x)))),
      h("tbody", {}, ...rows.map((r) => h("tr", {}, ...r.map((c) => h("td", {}, c instanceof Node ? c : String(c ?? ""))))))));
}

function schemaBlock(title, value, role) {
  return h("section", { class: "stack tight" },
    h("h3", {}, title),
    h("pre", { class: "ref-json", "data-role": role, tabindex: "0" }, pretty(value ?? {})));
}

// ---- 項目定義:id → {category, label, render} ------------------------------------------------

function buildItems(cat) {
  const items = new Map();
  const conn = cat.connection || {};
  const add = (id, category, labelText, render) => items.set(id, { id, category, label: labelText, render });

  add("conn-endpoint", "connection", T.conn_endpoint, () => [
    h("h2", {}, T.conn_endpoint),
    dl([[T.this_group, mono(conn.url)], [T.transport, mono(conn.transport)]]),
    h("h3", {}, T.group_urls),
    table([T.group, T.url], (conn.groups || []).map((g) => [mono(g.name), mono(g.url)])),
  ]);
  add("conn-auth", "connection", T.conn_auth, () => {
    const auth = conn.auth || {};
    return [
      h("h2", {}, T.conn_auth),
      dl([[T.auth_type, mono(auth.type)], [T.auth_header, mono(auth.header)], [T.auth_value, mono(auth.value)],
        [T.auth_env, mono(auth.env)]]),
      h("p", { class: "muted small" }, T.auth_note),
    ];
  });
  add("conn-stdio", "connection", T.conn_stdio, () => {
    const s = conn.stdio || {};
    return [
      h("h2", {}, T.conn_stdio),
      h("p", {}, s.description || ""),
      dl([[T.command, mono([s.command, ...(s.args || [])].join(" "))], [T.env, mono((s.env || []).join(", "))]]),
    ];
  });

  for (const tool of cat.tools || []) {
    add(`tool-${tool.name}`, "tools", tool.name, () => [
      h("h2", { class: "mono" }, tool.name),
      dl([[T.title, tool.title], [T.description, h("span", { "data-role": "description" }, tool.description || "")]]),
      schemaBlock(T.input_schema, tool.inputSchema, "input-schema"),
      schemaBlock(T.output_schema, tool.outputSchema, "output-schema"),
      tool.annotations ? schemaBlock(T.annotations, tool.annotations, "annotations") : null,
    ]);
  }

  for (const n of cat.notifications || []) {
    add(`notify-${n.method}`, "notifications", n.method, () => [
      h("h2", { class: "mono" }, n.method),
      dl([[T.description, n.description], [T.delivery, n.delivery]]),
    ]);
  }

  const errors = cat.errors || {};
  add("errors-http", "errors", T.err_http, () => [
    h("h2", {}, `${T.cat_errors}:${T.err_http}`),
    h("p", { class: "muted small" }, T.http_errors_note),
    table([T.code, T.status, T.format, T.description],
      (errors.http || []).map((e) => [mono(e.code), e.status, e.format, e.description])),
  ]);
  add("errors-tool", "errors", T.err_tool, () => [
    h("h2", {}, `${T.cat_errors}:${T.err_tool}`),
    h("p", { class: "muted small" }, T.tool_errors_note),
    table([T.code, T.description], (errors.tool || []).map((e) => [mono(e.code), e.description])),
  ]);
  add("errors-sdk", "errors", T.err_sdk, () => [
    h("h2", {}, `${T.cat_errors}:${T.err_sdk}`),
    h("p", { class: "muted small" }, T.sdk_errors_note),
    table([T.status, T.message, T.format, T.description],
      (errors.sdk_transport || []).map((e) => [e.status, mono(e.message), e.format, e.description])),
  ]);
  return items;
}

const CATEGORIES = Object.freeze([
  ["connection", T.cat_connection],
  ["tools", T.cat_tools],
  ["notifications", T.cat_notifications],
  ["errors", T.cat_errors],
]);

// ---- 繪製 ------------------------------------------------------------------------------------

function renderIndex() {
  const box = byId("ref-index");
  // 保留使用者已收合的類別
  const closed = new Set([...box.querySelectorAll("details:not([open])")].map((d) => d.dataset.category));
  clear(box);
  for (const [key, title] of CATEGORIES) {
    const list = h("ul", { class: "plain ref-items" });
    for (const item of state.items.values()) {
      if (item.category !== key) continue;
      list.append(h("li", {}, h("a", { href: `#${encodeURIComponent(item.id)}`, "data-item": item.id,
        class: item.category === "tools" || item.category === "notifications" ? "mono" : null }, item.label)));
    }
    if (key === "tools" && !list.childElementCount) list.append(h("li", { class: "muted small" }, T.no_tools));
    box.append(h("details", { "data-category": key, open: !closed.has(key) },
      h("summary", {}, title), list));
  }
}

function currentId() {
  const raw = window.location.hash.slice(1);
  if (!raw) return "";
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

function renderDetail() {
  const box = byId("ref-detail");
  clear(box);
  if (!state.items.size) return;
  const wanted = currentId();
  let item = state.items.get(wanted);
  const missing = wanted && !item;
  if (!item) item = state.items.values().next().value;
  if (missing) box.append(h("p", { class: "muted small", role: "status" }, T.not_found_item));
  box.setAttribute("data-item", item.id);
  box.append(...item.render().filter(Boolean));
  for (const a of byId("ref-index").querySelectorAll("a[data-item]")) {
    if (a.dataset.item === item.id) a.setAttribute("aria-current", "true");
    else a.removeAttribute("aria-current");
  }
  // 所選項目所在的類別若被收合,展開它
  const cur = byId("ref-index").querySelector(`a[aria-current="true"]`);
  const details = cur ? cur.closest("details") : null;
  if (details && !details.open) details.open = true;
}

async function loadCatalog() {
  const isLatest = catalogGuard();
  document.documentElement.removeAttribute("data-ref-ready");
  const detail = byId("ref-detail");
  setLoading(detail, true);
  try {
    const cat = await getJSON(withQuery("/api/mcp/catalog", { group: state.group }));
    if (!isLatest()) return;
    clearMessage("catalog");
    state.catalog = cat;
    state.items = buildItems(cat);
    setLoading(detail, false);
    renderIndex();
    renderDetail();
    document.documentElement.setAttribute("data-ref-ready", state.group);
  } catch (err) {
    if (!isLatest()) return;
    state.catalog = null;
    state.items = new Map();
    setLoading(detail, false);
    clear(detail);
    clear(byId("ref-index"));
    showError(err, { source: "catalog" });
    document.documentElement.setAttribute("data-ref-ready", "error");
  }
}

function syncQuery() {
  const q = state.group === "all" ? "" : `?${new URLSearchParams({ group: state.group })}`;
  history.replaceState(null, "", `${window.location.pathname}${q}${window.location.hash}`);
}

async function main() {
  initShell();
  const g = new URLSearchParams(window.location.search).get("group");
  if (g) state.group = g;
  window.addEventListener("hashchange", () => {
    if (state.items.size) renderDetail();
  });
  await fillGroupSelect(byId("ref-group"), { includeAll: true, current: state.group });
  byId("ref-group").addEventListener("change", (ev) => {
    state.group = ev.target.value;
    syncQuery();
    loadCatalog();
  });
  await loadCatalog();
}

main();

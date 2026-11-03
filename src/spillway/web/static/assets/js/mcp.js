// UI-5 MCP 設定(CHG-20261103-01 T7;決策 15,決策審議修訂 AM-12、AM-17;設計差異 #7)。
// - 群組選單(all 與各群組)× 工具分頁(5 種),資料取自 GET /api/mcp/config?group=&tool=;
//   顯示每個 variant 的標籤、scope、放置路徑與內容,另列 env 與 notes。
// - 「複製」用 Clipboard API;失敗或權限被拒時選取該段文字並提示按 ⌘C / Ctrl+C。
// - 「下載」只在 variant 有 filename 時提供:Blob(text/plain;charset=utf-8)→ <a download> 觸發 →
//   revokeObjectURL;檔名去除 / 與 \。
// - 畫面不顯示 token(設計稿的「內嵌 token」不做),只顯示需設定的環境變數名。
// - 網址 ?group=<g>&tool=<t> 決定初始選擇(只做 GET)。

import { getJSON, withQuery } from "./api.js";
import { byId, clear, h, setText } from "./dom.js";
import { clearMessage, codeLabel, fillGroupSelect, initShell, latest, setLoading, showError, showMessage } from "./ui.js";
import { label } from "./strings.js";

const TOOLS = Object.freeze([
  ["claude-code", "Claude Code"],
  ["codex", "Codex"],
  ["cursor", "Cursor"],
  ["gemini", "Gemini CLI"],
  ["stdio", "通用 stdio"],
]);

const SCOPE = Object.freeze({ user: "使用者層級", project: "專案層級" });
const FORMAT = Object.freeze({ command: "命令", json: "JSON", toml: "TOML" });
const SEVERITY = Object.freeze({ info: "說明", warn: "注意" });

const T = Object.freeze({
  copy: "複製",
  download: "下載檔案",
  copied: "已複製到剪貼簿",
  copy_failed: "無法寫入剪貼簿(權限被拒或不支援)。已選取文字,請按 ⌘C(macOS)或 Ctrl+C 複製。",
  run_in_terminal: "在終端機執行",
  place_at: "放置位置",
  file: "檔名",
  env_hint: "(於「設定」頁產生 token 後設定此環境變數)",
  no_tools: "此群組目前沒有可用的 tool",
  downloaded: "已下載",
});

const state = { group: "all", tool: "claude-code" };
const configGuard = latest();

// AM-12:檔名去除 / 與 \;去除後為空時用預設名
function safeFilename(name) {
  const cleaned = String(name ?? "").replace(/[/\\]/g, "");
  return cleaned || "spillway-mcp.txt";
}

// AM-12:Blob type 一律 text/plain;charset=utf-8;只經 <a download> 觸發,點擊後 revoke
function downloadText(filename, content) {
  const blob = new Blob([String(content)], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = h("a", { href: url, download: safeFilename(filename) });
  try {
    a.click();
  } finally {
    // 等點擊觸發的下載取得 Blob 之後再釋放
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}

function selectText(el) {
  const sel = window.getSelection();
  if (!sel) return;
  sel.removeAllRanges();
  const range = document.createRange();
  range.selectNodeContents(el);
  sel.addRange(range);
}

async function copyContent(pre, content, hint) {
  hint.hidden = true;
  try {
    if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error("no clipboard");
    await navigator.clipboard.writeText(content);
    showMessage(T.copied, "ok");
  } catch {
    selectText(pre);
    setText(hint, T.copy_failed);
    hint.hidden = false;
    pre.focus();
  }
}

function variantCard(v, index) {
  const pre = h("pre", { class: "wrap", tabindex: "0", "data-role": "content",
    "aria-label": v.label }, v.content);
  const hint = h("p", { class: "note warn", role: "status", "data-role": "copy-hint", hidden: true });
  const copyBtn = h("button", { type: "button", class: "btn primary", "data-role": "copy" }, T.copy);
  copyBtn.addEventListener("click", () => copyContent(pre, v.content, hint));
  const actions = [];
  if (v.filename) {
    const dl = h("button", { type: "button", class: "btn", "data-role": "download" }, T.download);
    dl.addEventListener("click", () => {
      downloadText(v.filename, v.content);
      showMessage(`${T.downloaded}:${safeFilename(v.filename)}`, "ok");
    });
    actions.push(dl);
  }
  actions.push(copyBtn);
  const where = v.path
    ? [h("dt", {}, T.place_at), h("dd", { class: "mono", "data-role": "path" }, v.path)]
    : [h("dt", {}, T.place_at), h("dd", { "data-role": "path" }, T.run_in_terminal)];
  const file = v.filename ? [h("dt", {}, T.file), h("dd", { class: "mono", "data-role": "filename" },
    safeFilename(v.filename))] : [];
  return h("article", { class: "mcp-variant", "data-variant": String(index) },
    h("div", { class: "row between" },
      h("h3", { "data-role": "label" }, v.label),
      h("div", { class: "row" },
        codeLabel(SCOPE, v.scope, { class: "badge b-muted", "data-role": "scope" }),
        codeLabel(FORMAT, v.format, { class: "badge b-api" }))),
    h("dl", {}, ...where, ...file),
    pre,
    hint,
    h("div", { class: "row end" }, ...actions));
}

function renderConfig(cfg) {
  const box = byId("mcp-variants");
  clear(box);
  box.append(...cfg.variants.map((v, i) => variantCard(v, i)));
  const env = byId("mcp-env");
  clear(env);
  for (const name of cfg.env || []) {
    env.append(h("span", { class: "mono", "data-role": "env" }, name), " ");
  }
  env.append(h("span", { class: "muted" }, T.env_hint));
  const notes = byId("mcp-notes");
  clear(notes);
  for (const n of cfg.notes || []) {
    notes.append(h("li", { class: `note ${n.severity === "warn" ? "warn" : "info"}`, role: "note",
      "data-severity": n.severity },
    h("strong", {}, `${label(SEVERITY, n.severity)}:`), n.text));
  }
}

function renderCatalog(cat) {
  setText(byId("mcp-endpoint"), cat.connection ? cat.connection.url : "");
  const list = byId("mcp-toollist");
  clear(list);
  const tools = cat.tools || [];
  if (!tools.length) {
    list.append(h("li", { class: "muted" }, T.no_tools));
    return;
  }
  for (const t of tools) {
    list.append(h("li", { "data-tool": t.name },
      h("div", { class: "stack tight" },
        h("span", { class: "mono mcp-toolname" }, t.name),
        h("span", { class: "muted small" }, t.description || ""))));
  }
}

function renderTabs() {
  const bar = byId("mcp-tools");
  clear(bar);
  for (const [key, labelText] of TOOLS) {
    const on = key === state.tool;
    const b = h("button", { type: "button", role: "tab", "aria-selected": on ? "true" : "false",
      "data-tool": key, tabindex: on ? "0" : "-1" }, labelText);
    b.addEventListener("click", () => {
      if (state.tool === key) return;
      state.tool = key;
      renderTabs();
      loadConfig();
    });
    b.addEventListener("keydown", (ev) => {
      const idx = TOOLS.findIndex(([k]) => k === key);
      let next = null;
      if (ev.key === "ArrowRight") next = TOOLS[(idx + 1) % TOOLS.length][0];
      if (ev.key === "ArrowLeft") next = TOOLS[(idx + TOOLS.length - 1) % TOOLS.length][0];
      if (next) {
        ev.preventDefault();
        state.tool = next;
        renderTabs();
        loadConfig();
        const target = bar.querySelector(`[data-tool="${CSS.escape(next)}"]`);
        if (target) target.focus();
      }
    });
    bar.append(b);
  }
}

async function loadConfig() {
  const isLatest = configGuard();
  const box = byId("mcp-variants");
  setLoading(box, true);
  document.documentElement.removeAttribute("data-mcp-ready");
  try {
    const [cfg, cat] = await Promise.all([
      getJSON(withQuery("/api/mcp/config", { group: state.group, tool: state.tool })),
      getJSON(withQuery("/api/mcp/catalog", { group: state.group })),
    ]);
    if (!isLatest()) return;
    clearMessage("config");
    setLoading(box, false);
    renderConfig(cfg);
    renderCatalog(cat);
    document.documentElement.setAttribute("data-mcp-ready", `${state.group}|${state.tool}`);
  } catch (err) {
    if (!isLatest()) return;
    setLoading(box, false);
    // 失敗時清掉上一個組合的內容,避免顯示與選單不符的舊資料
    for (const id of ["mcp-variants", "mcp-env", "mcp-notes", "mcp-endpoint", "mcp-toollist"]) clear(byId(id));
    showError(err, { source: "config" });
    document.documentElement.setAttribute("data-mcp-ready", "error");
  }
}

async function main() {
  initShell();
  const params = new URLSearchParams(window.location.search);
  const g = params.get("group");
  if (g) state.group = g;
  const t = params.get("tool");
  if (t && TOOLS.some(([k]) => k === t)) state.tool = t;
  renderTabs();
  await fillGroupSelect(byId("mcp-group"), { includeAll: true, current: state.group });
  byId("mcp-group").addEventListener("change", (ev) => {
    state.group = ev.target.value;
    loadConfig();
  });
  await loadConfig();
}

main();

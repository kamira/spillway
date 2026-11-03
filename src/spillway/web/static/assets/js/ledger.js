// UI-6 帳本(CHG-20261103-01 T4;決策 16、20;設計差異 #8)。
// - 篩選:起訖(本地日期)、route、成員、狀態、「只看 fallback」(= min_depth=1);任一欄位改變即重新查詢,
//   網址 query 與 `/api/ledger` 的請求參數一致(一律經 URLSearchParams 編碼)。
// - 起訖以本地日期輸入,送出時換算成完整 ISO:起 = 該日本地 00:00;訖 = 隔日本地 00:00(不含)。
// - 分頁;「匯出 CSV」為帶相同篩選的 /api/ledger/export.csv 連結,total > 100000 時停用並提示縮小範圍。
// - 點選列在側欄顯示內容(GET …/payload;410「已過保存期限」、404「此次未記錄內容」;redacted/truncated 標示;
//   「保存至」= ts + context_retention_days;「複製」)。內容以 <pre> 的 textContent 顯示(決策 8)。
// - 成員已刪除的列(孤兒列)照原值顯示 deployment_id 並加「(已刪除)」。

import { getJSON, withQuery } from "./api.js";
import { byId, clear, h, setAttr, setText } from "./dom.js";
import { STATUS, TEXT, label } from "./strings.js";
import { codeLabel, errorText, fmtLocal, initShell, showError, showMessage, timeEl } from "./ui.js";

const CSV_MAX_ROWS = 100000;
const DAY_MS = 86400000;
const FILTER_KEYS = ["from", "to", "deployment", "route", "status", "min_depth"];

const T = Object.freeze({
  dash: "—",
  deleted: "(已刪除)",
  empty: "沒有符合條件的紀錄",
  pageInfo: (page, pages, total) => `第 ${page} / ${Math.max(pages, 1)} 頁,共 ${total.toLocaleString("en-US")} 列`,
  title: "紀錄內容",
  expired: "已過保存期限",
  notRecorded: "此次未記錄內容",
  truncated: "已截斷",
  notTruncated: "未截斷",
  redacted: "已遮罩",
  keptUntil: "保存至 ",
  request: "請求",
  response: "回應",
  noResponse: "(沒有回應內容)",
  copy: "複製",
  copied: "已複製到剪貼簿",
  copyFailed: "無法寫入剪貼簿",
  errorLabel: "錯誤:",
  cliNote: "CLI 成員只記錄送入的 prompt 與最終結果,不含工具呼叫過程。",
  exportDisabled: "符合的列超過 100,000 列,請縮小範圍後再匯出",
});

const state = {
  filters: {},       // 與 /api/ledger 參數同名(from、to 為完整 ISO)
  page: 1,
  deployments: new Set(),
  deploymentsLoaded: false,   // 成員清單未載入成功時不判定孤兒列
  retentionDays: null,
  lastPage: 1,                // 最後一次成功回應的 data.page(上/下一頁由它 ±1)
  loadSeq: 0,
  payloadSeq: 0,
  selected: null,
};

// ---- 日期換算(本地日期 ↔ ISO)-------------------------------------------------------------------

function pad(n) {
  return String(n).padStart(2, "0");
}

function isoOf(d) {
  return d.toISOString().replace(/\.\d{3}Z$/, "Z");
}

function parseLocalDate(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
  if (!m) return null;
  return [Number(m[1]), Number(m[2]) - 1, Number(m[3])];
}

// 起:該日本地 00:00
function fromDateToIso(value) {
  const p = parseLocalDate(value);
  return p ? isoOf(new Date(p[0], p[1], p[2], 0, 0, 0, 0)) : null;
}

// 訖:隔日本地 00:00(不含)
function toDateToIso(value) {
  const p = parseLocalDate(value);
  return p ? isoOf(new Date(p[0], p[1], p[2] + 1, 0, 0, 0, 0)) : null;
}

function localDateOf(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function isoToFromDate(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : localDateOf(d);
}

function isoToToDate(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return localDateOf(new Date(d.getFullYear(), d.getMonth(), d.getDate() - 1));
}

// ---- 格式化 ---------------------------------------------------------------------------------------

function fmtInt(n) {
  return n === null || n === undefined ? T.dash : Number(n).toLocaleString("en-US");
}

function fmtCost(n) {
  return n === null || n === undefined ? T.dash : `$${Number(n).toFixed(4)}`;
}

function fmtLatency(ms) {
  return ms === null || ms === undefined ? T.dash : `${(Number(ms) / 1000).toFixed(1)} s`;
}

function statusBadge(status) {
  const cls = { ok: "b-ok", error: "b-err", quota_exhausted: "b-warn", rate_limited: "b-warn",
    skipped_precheck: "b-muted" }[status] || "b-muted";
  return codeLabel(STATUS, status, { class: `badge ${cls}` });
}

function memberText(depId) {
  if (depId === null || depId === undefined) return [h("span", { class: "muted" }, T.dash)];
  const nodes = [h("span", { class: "mono" }, depId)];
  if (state.deploymentsLoaded && !state.deployments.has(depId)) {
    nodes.push(h("span", { class: "muted ledger-orphan" }, T.deleted));
  }
  return nodes;
}

// ---- 篩選與網址 ----------------------------------------------------------------------------------

function apiParams(withPage) {
  const p = {};
  for (const k of FILTER_KEYS) {
    if (state.filters[k] !== undefined && state.filters[k] !== null && state.filters[k] !== "") p[k] = state.filters[k];
  }
  if (withPage && state.page > 1) p.page = state.page;
  return p;
}

function readFiltersFromForm() {
  const f = {};
  const from = fromDateToIso(byId("f-from").value);
  const to = toDateToIso(byId("f-to").value);
  if (from) f.from = from;
  if (to) f.to = to;
  for (const k of ["route", "deployment", "status"]) {
    const v = byId(`f-${k}`).value;
    if (v) f[k] = v;
  }
  if (byId("f-fallback").checked) f.min_depth = "1";
  return f;
}

function ensureOption(select, value) {
  if (!value) return;
  const exists = Array.from(select.options).some((o) => o.value === value);
  if (!exists) select.append(h("option", { value }, value));
}

function readFiltersFromUrl() {
  const q = new URLSearchParams(window.location.search);
  const f = {};
  for (const k of FILTER_KEYS) {
    const v = q.get(k);
    if (v !== null && v !== "") f[k] = v;
  }
  for (const k of ["from", "to"]) {
    if (f[k] !== undefined && Number.isNaN(new Date(f[k]).getTime())) delete f[k];
  }
  const page = Number.parseInt(q.get("page") || "1", 10);
  state.filters = f;
  state.page = Number.isFinite(page) && page >= 1 ? page : 1;
  byId("f-from").value = f.from ? isoToFromDate(f.from) : "";
  byId("f-to").value = f.to ? isoToToDate(f.to) : "";
  for (const k of ["route", "deployment", "status"]) {
    ensureOption(byId(`f-${k}`), f[k]);
    byId(`f-${k}`).value = f[k] || "";
  }
  byId("f-fallback").checked = f.min_depth !== undefined && Number(f.min_depth) >= 1;
}

function syncUrl() {
  history.replaceState(null, "", withQuery("/ui/ledger.html", apiParams(true)));
}

function disableExport(title) {
  const link = byId("export-csv");
  link.removeAttribute("href");
  link.setAttribute("aria-disabled", "true");
  if (title) link.setAttribute("title", title);
  else link.removeAttribute("title");
}

// 只在 /api/ledger 成功後呼叫;載入中與載入失敗時連結保持停用(不沿用舊篩選)
function updateExport(total) {
  const link = byId("export-csv");
  const tooMany = total > CSV_MAX_ROWS;
  if (tooMany) {
    disableExport(T.exportDisabled);
  } else {
    setAttr(link, "href", withQuery("/api/ledger/export.csv", apiParams(false)));
    link.removeAttribute("aria-disabled");
    link.removeAttribute("title");
  }
  byId("export-hint").hidden = !tooMany;
}

// ---- 清單 -----------------------------------------------------------------------------------------

function renderRows(items) {
  const body = byId("ledger-body");
  clear(body);
  if (!items.length) {
    body.append(h("tr", {}, h("td", { colspan: "12", class: "empty" }, T.empty)));
    return;
  }
  for (const r of items) {
    const tr = h("tr", { class: "clickable", tabindex: "0", "data-ledger-id": r.id,
      "aria-selected": state.selected === r.id ? "true" : "false" },
    h("td", { class: "nowrap" }, timeEl(r.ts)),
    h("td", { class: "mono" }, r.route_name ?? T.dash),
    h("td", { class: "ledger-member-cell" }, ...memberText(r.deployment_id)),
    h("td", { class: "num" }, r.slot ?? T.dash),
    h("td", {}, r.effort ?? T.dash),
    h("td", { class: "num" }, `${fmtInt(r.est_tokens_in)} / ${fmtInt(r.est_tokens_out_p90)}`),
    h("td", { class: "num" }, `${fmtInt(r.tokens_in)} / ${fmtInt(r.tokens_out)}`),
    h("td", { class: "num" }, fmtCost(r.est_cost_usd_p90)),
    h("td", { class: "num" }, fmtCost(r.cost_usd)),
    h("td", { class: "ledger-status-cell" }, statusBadge(r.status),
      r.error ? h("div", { class: "small muted ledger-error" }, r.error) : null),
    h("td", { class: "num nowrap" }, fmtLatency(r.latency_ms)),
    h("td", { class: "num" }, r.fallback_depth ?? 0));
    tr.addEventListener("click", () => openPayload(r));
    tr.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openPayload(r);
      }
    });
    body.append(tr);
  }
}

async function load() {
  const seq = ++state.loadSeq;
  const table = byId("ledger-table");
  table.setAttribute("aria-busy", "true");
  table.removeAttribute("data-loaded");
  byId("page-prev").disabled = true;
  byId("page-next").disabled = true;
  disableExport(null);
  byId("export-hint").hidden = true;
  syncUrl();
  try {
    const data = await getJSON(withQuery("/api/ledger", apiParams(true)));
    if (seq !== state.loadSeq) return;
    const lastPage = Math.max(Number(data.pages) || 0, 1);
    if (Number(data.page) > lastPage) {
      // 網址的 page 超過最後一頁:改到最後一頁重新查詢(網址一併同步)
      state.page = lastPage;
      load();
      return;
    }
    state.lastPage = Number(data.page);
    renderRows(Array.isArray(data.items) ? data.items : []);
    setText(byId("page-info"), T.pageInfo(data.page, data.pages, data.total));
    byId("page-prev").disabled = data.page <= 1;
    byId("page-next").disabled = data.page >= data.pages;
    updateExport(Number(data.total));
  } catch (err) {
    if (seq === state.loadSeq) showError(err);
  } finally {
    if (seq === state.loadSeq) {
      table.removeAttribute("aria-busy");
      table.setAttribute("data-loaded", "true");
    }
  }
}

function onFilterChange() {
  state.filters = readFiltersFromForm();
  state.page = 1;
  load();
}

// ---- 內容側欄 -------------------------------------------------------------------------------------

function markSelected(id) {
  state.selected = id;
  for (const tr of byId("ledger-body").querySelectorAll("tr[data-ledger-id]")) {
    tr.setAttribute("aria-selected", tr.getAttribute("data-ledger-id") === String(id) ? "true" : "false");
  }
}

function keptUntilIso(ts) {
  const t = Date.parse(ts);
  if (Number.isNaN(t) || state.retentionDays === null || Number(state.retentionDays) <= 0) return null;
  return isoOf(new Date(t + Number(state.retentionDays) * DAY_MS));
}

function payloadNotice(text, kind) {
  return h("p", { class: `ledger-payload-notice ${kind}`, "data-role": "payload-notice" }, text);
}

function errorLine(row) {
  return h("p", { class: "small" }, T.errorLabel, h("span", { class: "ledger-error" }, row.error));
}

function renderPayload(row, p) {
  const meta = byId("payload-meta");
  const body = byId("payload-body");
  clear(meta);
  clear(body);
  if (row.error) body.append(errorLine(row));
  meta.append(
    h("span", { class: p.truncated ? "badge b-warn" : "badge b-ok", "data-role": "truncated" },
      p.truncated ? T.truncated : T.notTruncated),
    p.redacted ? h("span", { class: "badge b-warn", "data-role": "redacted" }, T.redacted) : null);
  const kept = keptUntilIso(row.ts);
  if (kept) meta.append(h("span", { class: "muted", "data-role": "kept-until" }, T.keptUntil, timeEl(kept)));

  const texts = { request: p.request_json ?? "", response: p.response_text ?? "" };
  const pre = h("pre", { class: "wrap ledger-payload-text", id: "payload-text" });
  const tabs = h("div", { class: "tabs", role: "tablist", "aria-label": T.title });
  let current = "request";
  const show = (which) => {
    current = which;
    for (const b of tabs.querySelectorAll("button")) {
      b.setAttribute("aria-selected", b.getAttribute("data-tab") === which ? "true" : "false");
    }
    setText(pre, which === "response" && p.response_text === null ? T.noResponse : texts[which]);
    pre.setAttribute("data-tab", which);
  };
  for (const [key, text] of [["request", T.request], ["response", T.response]]) {
    const b = h("button", { type: "button", role: "tab", "data-tab": key, "aria-selected": "false" }, text);
    b.addEventListener("click", () => show(key));
    tabs.append(b);
  }
  const copy = h("button", { type: "button", class: "btn sm", id: "payload-copy" }, T.copy);
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(texts[current]);
      showMessage(T.copied, "ok");
    } catch {
      showMessage(T.copyFailed, "error");
    }
  });
  body.append(tabs, pre,
    h("div", { class: "row between" }, h("span", { class: "muted small" }, T.cliNote), copy));
  show("request");
}

async function openPayload(row) {
  const seq = ++state.payloadSeq;
  markSelected(row.id);
  const panel = byId("payload-panel");
  const meta = byId("payload-meta");
  const body = byId("payload-body");
  setText(byId("payload-title"), `${T.title} · ${fmtLocal(row.ts)} · ${row.deployment_id ?? T.dash}`);
  panel.setAttribute("data-ledger-id", String(row.id));
  panel.removeAttribute("data-loaded");
  clear(meta);
  clear(body);
  if (row.error) body.append(errorLine(row));
  const loading = h("p", { class: "loading" }, TEXT.loading);
  body.append(loading);
  panel.hidden = false;
  try {
    const p = await getJSON(`/api/ledger/${encodeURIComponent(row.id)}/payload`);
    if (seq !== state.payloadSeq) return;
    renderPayload(row, p);
  } catch (err) {
    if (seq !== state.payloadSeq) return;
    loading.remove();
    if (err && err.status === 410) body.append(payloadNotice(T.expired, "warn-text"));
    else if (err && err.status === 404) body.append(payloadNotice(T.notRecorded, "muted"));
    else body.append(payloadNotice(errorText(err), "danger-text"));
  } finally {
    if (seq === state.payloadSeq) panel.setAttribute("data-loaded", "true");
  }
}

// 關閉(與 pagehide)時清空內容,不把 prompt 與回應留在 DOM(含 bfcache)
function closePayload() {
  state.payloadSeq += 1;
  const panel = byId("payload-panel");
  panel.hidden = true;
  panel.removeAttribute("data-ledger-id");
  panel.removeAttribute("data-loaded");
  setText(byId("payload-title"), T.title);
  clear(byId("payload-meta"));
  clear(byId("payload-body"));
  markSelected(null);
}

// ---- 啟動 -----------------------------------------------------------------------------------------

function fillSelect(select, values, labelOf = (v) => v) {
  for (const v of values) select.append(h("option", { value: v }, labelOf(v)));
}

async function main() {
  initShell();
  const form = byId("filters");
  form.addEventListener("submit", (e) => e.preventDefault());
  form.addEventListener("change", onFilterChange);
  form.addEventListener("reset", () => setTimeout(onFilterChange, 0));
  byId("page-prev").addEventListener("click", () => { state.page = Math.max(1, state.lastPage - 1); load(); });
  byId("page-next").addEventListener("click", () => { state.page = state.lastPage + 1; load(); });
  byId("payload-close").addEventListener("click", closePayload);
  window.addEventListener("pagehide", closePayload);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !byId("payload-panel").hidden) closePayload();
  });
  byId("export-csv").addEventListener("click", (e) => {
    if (byId("export-csv").getAttribute("aria-disabled") === "true") e.preventDefault();
  });
  fillSelect(byId("f-status"), Object.keys(STATUS), (v) => label(STATUS, v));
  // 三個選單資料各自載入:其中一個失敗不影響其他
  const [routes, deps, ctx] = await Promise.allSettled([
    getJSON("/api/routes"), getJSON("/api/deployments"), getJSON("/api/settings/context")]);
  if (routes.status === "fulfilled") fillSelect(byId("f-route"), (routes.value.routes || []).map((r) => r.name));
  if (deps.status === "fulfilled") {
    const depIds = (deps.value.deployments || []).map((d) => d.id);
    state.deployments = new Set(depIds);
    state.deploymentsLoaded = true;
    fillSelect(byId("f-deployment"), depIds);
  }
  if (ctx.status === "fulfilled") state.retentionDays = ctx.value.context_retention_days;
  const failed = [routes, deps, ctx].find((r) => r.status === "rejected");
  if (failed) showError(failed.reason);
  readFiltersFromUrl();
  await load();
}

main();

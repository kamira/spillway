// 共用畫面元件(CHG-20261103-01 決策 20;AM-12、AM-17)。
// - initShell():導覽底部填入 location.host(127.0.0.1:<port>),完成後 <html data-shell="ready">(測試等待點)。
// - 頂端訊息列:showMessage(text, kind, {source})、showError(err, {source})、clearMessage(source);
//   network_error 顯示「無法連線到 spillway」;畫面只顯示 error.message,不解析 9.1.2 的 path 格式。
//   帶 source 的錯誤是「黏著」的:只有同一 source 的 clearMessage(source)(或使用者按 ×)才會清掉;
//   不帶 source 的訊息照舊由任何 clearMessage() 清除。
// - confirmDialog({title, body, confirmLabel, cancelLabel, danger}) → Promise<boolean>:<dialog> 實作,
//   預設焦點在「取消」,Esc 等於取消。
// - 載入中:setLoading(el, on);送出中停用按鈕:withBusy(button, fn)。
// - 格式化:toIsoZ(date)、fmtLocal(iso)、fmtUTC(iso)、timeEl(iso)、fmtUSD(n)(負值 -$x)、fmtInt(n);
//   代碼標籤:codeLabel(table, value)、kindBadge(kind)、statusBadge(status)。
// - 表單與清單:field(label, control, {hint, id})、parseList(text)、tagInput({...})、fillGroupSelect(select, {...})、
//   record_context 三態 recordChoice / recordValue / recordLabel / recordSelect。
// - 連結與摘要:memberHref(id)、targetSummary(target)。
// - latest():過時回應守門(只讓最後一次請求的回應生效)。
// - navigate(path, {replace}):只接受同源相對路徑(經 h() 的網址檢查),以 <a> 觸發導覽;replace 時以
//   history.replaceState 換掉目前的歷史項再重新載入(返回鍵不會回到原頁)。
// 不輸出請求或回應內容到 console。

import { getJSON } from "./api.js";
import { byId, clear, h, setText } from "./dom.js";
import { KIND, RECORD_CONTEXT, STATUS, TEXT, label } from "./strings.js";

export function initShell() {
  const host = byId("nav-host");
  if (host) setText(host, location.host);
  document.documentElement.setAttribute("data-shell", "ready");
}

// ---- 訊息列 ----------------------------------------------------------------------------------

function messageBar() {
  let bar = byId("msgbar");
  if (!bar) {
    bar = h("div", { id: "msgbar", class: "msgbar", role: "status", "aria-live": "polite", hidden: true });
    const main = document.querySelector("main") || document.body;
    main.prepend(bar);
  }
  return bar;
}

export function showMessage(text, kind = "info", { source = null } = {}) {
  const bar = messageBar();
  bar.className = `msgbar msg-${kind}`;
  if (kind === "error" && source) bar.setAttribute("data-source", source);
  else bar.removeAttribute("data-source");
  bar.setAttribute("role", kind === "error" ? "alert" : "status");
  clear(bar);
  bar.append(h("span", { class: "msg-text" }, String(text)),
    h("button", { type: "button", class: "msg-close", "aria-label": TEXT.close }, "×"));
  bar.querySelector(".msg-close").addEventListener("click", () => hideMessage(bar));
  bar.hidden = false;
  return bar;
}

export function errorText(err) {
  if (err && err.status === 0 && err.code === "network_error") return TEXT.network_error;
  if (err && typeof err.message === "string" && err.message) return err.message;
  return TEXT.http_error;
}

export function showError(err, { source = null } = {}) {
  return showMessage(errorText(err), "error", { source });
}

function hideMessage(bar) {
  clear(bar);
  bar.removeAttribute("data-source");
  bar.hidden = true;
}

// 帶 source 的錯誤只由同一 source 清除(某個載入器成功時不會蓋掉另一個載入器的錯誤)
export function clearMessage(source = null) {
  const bar = byId("msgbar");
  if (!bar) return;
  const owner = bar.getAttribute("data-source");
  if (owner && owner !== source) return;
  hideMessage(bar);
}

// ---- 確認對話框 ------------------------------------------------------------------------------

export function confirmDialog({ title, body, confirmLabel = TEXT.confirm, cancelLabel = TEXT.cancel,
  danger = false } = {}) {
  return new Promise((resolve) => {
    const cancel = h("button", { type: "button", class: "btn", "data-role": "cancel" }, cancelLabel);
    const ok = h("button", { type: "button", class: danger ? "btn danger" : "btn primary",
      "data-role": "confirm" }, confirmLabel);
    const bodyNodes = body === undefined || body === null ? [] : [body];
    const dialog = h("dialog", { class: danger ? "dialog dialog-danger" : "dialog",
      "aria-labelledby": "confirm-dialog-title" },
    h("h2", { id: "confirm-dialog-title", class: "dialog-title" }, String(title ?? "")),
    h("div", { class: "dialog-body" }, ...bodyNodes.map((b) => (typeof b === "string" ? h("p", {}, b) : b))),
    h("div", { class: "dialog-actions" }, cancel, ok));
    let result = false;
    cancel.addEventListener("click", () => { result = false; dialog.close(); });
    ok.addEventListener("click", () => { result = true; dialog.close(); });
    dialog.addEventListener("cancel", () => { result = false; });
    dialog.addEventListener("close", () => {
      dialog.remove();
      resolve(result);
    });
    document.body.append(dialog);
    dialog.showModal();
    cancel.focus();
  });
}

// ---- 載入中與送出中 --------------------------------------------------------------------------

export function setLoading(el, on = true) {
  if (on) {
    el.setAttribute("aria-busy", "true");
    clear(el);
    el.append(h("span", { class: "loading" }, TEXT.loading));
  } else {
    el.removeAttribute("aria-busy");
    const spinner = el.querySelector(".loading");
    if (spinner) spinner.remove();
  }
  return el;
}

export async function withBusy(button, fn) {
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    return await fn();
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
}

// ---- 格式化 ---------------------------------------------------------------------------------

const LOCAL_FMT = new Intl.DateTimeFormat("zh-TW", {
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  second: "2-digit", hour12: false,
});

export function fmtLocal(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : LOCAL_FMT.format(d);
}

// Date → 不含毫秒的 UTC ISO(2026-11-02T16:30:00Z)
export function toIsoZ(d) {
  return d.toISOString().replace(/\.\d{3}Z$/, "Z");
}

export function fmtUTC(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : `${toIsoZ(d)} (UTC)`;
}

// 本地時間顯示,UTC 放在 title
export function timeEl(iso) {
  return h("time", { datetime: iso || "", title: fmtUTC(iso) }, fmtLocal(iso));
}

// 負值顯示為 -$0.50(不是 $-0.50)
export function fmtUSD(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "";
  const fixed = Number(n).toFixed(2);
  return fixed.startsWith("-") ? `-$${fixed.slice(1)}` : `$${fixed}`;
}

export function fmtInt(n) {
  return n === null || n === undefined ? TEXT.dash : Number(n).toLocaleString("en-US");
}

// 代碼 → 繁中標籤,原值放在 title
export function codeLabel(table, value, attrs = {}) {
  return h("span", { ...attrs, title: value ?? "" }, label(table, value));
}

export function kindBadge(kind) {
  const cls = { cli: "b-cli", api: "b-api", local: "b-local" }[kind] || "b-muted";
  return codeLabel(KIND, kind, { class: `badge ${cls}` });
}

// 帳本與呼叫結果(status)的 badge
export function statusBadge(status) {
  const cls = { ok: "b-ok", error: "b-err", quota_exhausted: "b-warn", rate_limited: "b-warn",
    skipped_precheck: "b-muted" }[status] || "b-muted";
  return codeLabel(STATUS, status, { class: `badge ${cls}` });
}

// ---- 表單與清單 -----------------------------------------------------------------------------

// 表單欄位:<label class="f"><span class="field-label">標籤</span>控制項<span class="hint">說明</span></label>
export function field(labelText, control, { hint = null, id = null } = {}) {
  return h("label", { class: "f", for: id }, h("span", { class: "field-label" }, labelText), control,
    hint ? h("span", { class: "hint" }, hint) : null);
}

// 以逗號或空白分隔的清單 → 去重後的陣列(保留順序)
export function parseList(text) {
  const out = [];
  for (const t of String(text || "").split(/[,\s]+/)) {
    if (t && !out.includes(t)) out.push(t);
  }
  return out;
}

// tag chip 輸入:chips 容器 + 文字欄 + 「+ tag」按鈕(Enter 或按鈕加入、× 移除)。
// 回傳各部分由呼叫端排版;tags() 取目前的值,commit() 把文字欄的內容加入(送出前呼叫)。
// onChange(tags) 在加入(文字欄非空)或移除之後呼叫。
export function tagInput({ tags = [], onChange = null, chipClass = "chip",
  removeLabel = (t) => `${TEXT.tag_remove} ${t}`, chipsTag = "div", chipsAttrs = {}, inputAttrs = {} } = {}) {
  let current = [...tags];
  const chips = h(chipsTag, { class: "row", ...chipsAttrs });
  const input = h("input", { type: "text", autocomplete: "off", placeholder: TEXT.tag_placeholder, ...inputAttrs });
  const button = h("button", { type: "button", class: "btn sm" }, TEXT.tag_add);
  function render() {
    clear(chips);
    if (!current.length) chips.append(h("span", { class: "muted" }, TEXT.tag_none));
    for (const t of current) {
      const x = h("button", { type: "button", class: "chip-x", "aria-label": removeLabel(t) }, "×");
      x.addEventListener("click", () => {
        current = current.filter((v) => v !== t);
        changed();
      });
      chips.append(h("span", { class: chipClass, "data-tag": t }, t, x));
    }
  }
  function changed() {
    render();
    if (onChange) onChange([...current]);
  }
  function commit() {
    const t = input.value.trim();
    if (!t) return;
    if (!current.includes(t)) current.push(t);
    input.value = "";
    changed();
  }
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      commit();
    }
  });
  button.addEventListener("click", commit);
  render();
  return { chips, input, button, commit, tags: () => [...current] };
}

// 群組選單:includeAll 時第一項為 all;current 不在清單時仍列出原值;沒有任何群組、不含 all 且沒有 current 時
// 顯示 emptyLabel(值為空字串)。/api/groups 失敗時以 source "groups" 顯示錯誤。回傳群組名稱。
export async function fillGroupSelect(select, { includeAll = false, current = null, emptyLabel = null } = {}) {
  clear(select);
  if (includeAll) select.append(h("option", { value: "all" }, TEXT.all_group));
  let names = [];
  try {
    const data = await getJSON("/api/groups");
    names = (data.groups || []).map((g) => g.name);
  } catch (err) {
    showError(err, { source: "groups" });
  }
  for (const n of names) select.append(h("option", { value: n }, n));
  if (current && !(includeAll && current === "all") && !names.includes(current)) {
    select.append(h("option", { value: current }, current));
  }
  if (!names.length && !includeAll && !current && emptyLabel) select.append(h("option", { value: "" }, emptyLabel));
  if (current) select.value = current;
  return names;
}

// record_context 三態:null / true / false ↔ 選單值 inherit / on / off
export function recordChoice(value) {
  if (value === null || value === undefined) return "inherit";
  return value ? "on" : "off";
}

export function recordValue(choice) {
  return { inherit: null, on: true, off: false }[choice] ?? null;
}

export function recordLabel(value) {
  return RECORD_CONTEXT[recordChoice(value)];
}

export function recordSelect(attrs, value = null) {
  const sel = h("select", attrs, ...Object.entries(RECORD_CONTEXT).map(([k, l]) => h("option", { value: k }, l)));
  sel.value = recordChoice(value);
  return sel;
}

// ---- 連結與摘要 -----------------------------------------------------------------------------

export function memberHref(id) {
  return `/ui/members.html#${encodeURIComponent(id)}`;
}

// route target 的一行摘要(群組 <name> / tags all: …;any: …)
export function targetSummary(target) {
  if (target && typeof target.group === "string") return `${TEXT.target_group} ${target.group}`;
  const sel = (target && target.tags) || {};
  const parts = ["all", "any", "none"].filter((k) => sel[k] && sel[k].length).map((k) => `${k}: ${sel[k].join(", ")}`);
  return parts.length ? `tags ${parts.join(";")}` : TEXT.target_all_tags;
}

// ---- 過時回應守門 ---------------------------------------------------------------------------

// const guard = latest(); … const isLatest = guard(); await …; if (!isLatest()) return;
// guard.invalidate() 讓進行中的請求全部過時(例如關閉側欄)。
export function latest() {
  let seq = 0;
  const next = () => {
    const mine = ++seq;
    return () => mine === seq;
  };
  next.invalidate = () => { seq += 1; };
  return next;
}

// ---- 導覽 -----------------------------------------------------------------------------------

// 決策 8 的靜態掃描禁止直接改 location;改由 h() 建一個 <a>(經同源網址檢查)再 click() 觸發導覽,
// 不放進 DOM。path 必須以 /ui/ 或 ./ 開頭(或 #、?)。
// replace:以 history.replaceState 換掉目前的歷史項(同源檢查同上)後重新載入,返回鍵不會回到原頁。
export function navigate(path, { replace = false } = {}) {
  const a = h("a", { href: path });
  if (replace) {
    history.replaceState(null, "", a.getAttribute("href"));
    window.location.reload();
    return;
  }
  a.click();
}

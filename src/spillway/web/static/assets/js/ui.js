// 共用畫面元件(CHG-20261103-01 決策 20;AM-12、AM-17)。
// - initShell():導覽底部填入 location.host(127.0.0.1:<port>),完成後 <html data-shell="ready">(測試等待點)。
// - 頂端訊息列:showMessage(text, kind)、showError(err)、clearMessage();network_error 顯示「無法連線到 spillway」;
//   畫面只顯示 error.message,不解析 9.1.2 的 path 格式。
// - confirmDialog({title, body, confirmLabel, cancelLabel, danger}) → Promise<boolean>:<dialog> 實作,
//   預設焦點在「取消」,Esc 等於取消。
// - 載入中:setLoading(el, on);送出中停用按鈕:withBusy(button, fn)。
// - 格式化:fmtLocal(iso)、fmtUTC(iso)、timeEl(iso)、fmtUSD(n);代碼標籤:codeLabel(table, value)、kindBadge(kind)。
// - navigate(path):只接受同源相對路徑(經 h() 的網址檢查),以 <a> 觸發導覽。
// 不輸出請求或回應內容到 console。

import { byId, clear, h, setText } from "./dom.js";
import { KIND, TEXT, label } from "./strings.js";

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

export function showMessage(text, kind = "info") {
  const bar = messageBar();
  bar.className = `msgbar msg-${kind}`;
  bar.setAttribute("role", kind === "error" ? "alert" : "status");
  clear(bar);
  bar.append(h("span", { class: "msg-text" }, String(text)),
    h("button", { type: "button", class: "msg-close", "aria-label": TEXT.close }, "×"));
  bar.querySelector(".msg-close").addEventListener("click", clearMessage);
  bar.hidden = false;
  return bar;
}

export function errorText(err) {
  if (err && err.status === 0 && err.code === "network_error") return TEXT.network_error;
  if (err && typeof err.message === "string" && err.message) return err.message;
  return TEXT.http_error;
}

export function showError(err) {
  return showMessage(errorText(err), "error");
}

export function clearMessage() {
  const bar = byId("msgbar");
  if (bar) {
    clear(bar);
    bar.hidden = true;
  }
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

export function fmtUTC(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : `${d.toISOString().replace(".000Z", "Z")} (UTC)`;
}

// 本地時間顯示,UTC 放在 title
export function timeEl(iso) {
  return h("time", { datetime: iso || "", title: fmtUTC(iso) }, fmtLocal(iso));
}

export function fmtUSD(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "";
  return `$${Number(n).toFixed(2)}`;
}

// 代碼 → 繁中標籤,原值放在 title
export function codeLabel(table, value, attrs = {}) {
  return h("span", { ...attrs, title: value ?? "" }, label(table, value));
}

export function kindBadge(kind) {
  const cls = { cli: "b-cli", api: "b-api", local: "b-local" }[kind] || "b-muted";
  return codeLabel(KIND, kind, { class: `badge ${cls}` });
}

// ---- 導覽 -----------------------------------------------------------------------------------

export function navigate(path) {
  h("a", { href: path }).click();
}

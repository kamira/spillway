// UI-7 設定(CHG-20261103-01 T8;決策 17,決策審議修訂 AM-9、AM-12、AM-13、AM-15;設計差異 #9)。
// ① 總覽:版本、資料目錄、埠號(唯讀,附可複製的 `spillway serve --port <N>`)、token「已產生/未產生」。
// ② context 設定:預設記錄 context、保存天數(0 = 不保存)、單筆上限(KB;1 KB = 1024 bytes,送出時換回 bytes);
//    另顯示內容筆數與總大小。
// ③ 匯出:靜態連結 /api/config/export(下載)。④ 匯入:見 import-dialog.js。
// ⑤ token(#token):確認後 POST /api/settings/token/rotate,新 token 只在對話框顯示一次(見 openTokenDialog)。
// 頁面載入時只發 GET;網址的 hash(#token)只用於捲動定位(決策 8)。

import { getJSON, sendJSON } from "./api.js";
import { byId, h, setText } from "./dom.js";
import { TEXT } from "./strings.js";
import { clearMessage, confirmDialog, initShell, showError, showMessage, withBusy } from "./ui.js";
import { mountImport } from "./import-dialog.js";

export const KB = 1024;

// KB ⇄ bytes(1 KB = 1024 bytes;四捨五入到整數 byte)
export function kbToBytes(kb) {
  return Math.round(Number(kb) * KB);
}

export function bytesToKb(bytes) {
  return Number(bytes) / KB;
}

const T = Object.freeze({
  issued: "已產生",
  not_issued: "未產生",
  copy: "複製",
  copied: "已複製到剪貼簿",
  copy_failed: "無法寫入剪貼簿(權限被拒或不支援)。已選取文字,請按 ⌘C(macOS)或 Ctrl+C 複製。",
  stats: (n, size) => `目前 ${n.toLocaleString("en-US")} 筆 · ${size}`,
  ctx_saved: "已儲存 context 設定",
  err_days: "保存天數必須是 0 以上的整數",
  err_kb: "單筆上限必須是數字(KB)",
  // token(AM-12)
  first_title: "產生存取 token",
  first_body: "產生存取 token。產生後只顯示一次,請複製後設定為環境變數 SPILLWAY_TOKEN。",
  first_confirm: "產生",
  rotate_title: TEXT.rotate_token,
  rotate_body: "舊 token 立即失效,使用它的 MCP 設定都需更新。",
  rotate_confirm: "重新產生",
  token_title: "新的存取 token",
  token_label: "存取 token",
  token_once: "關閉後無法再次查看;剪貼簿可能被同步或留存。",
  token_export: "設定環境變數(與 token 同時顯示、同時清除):",
  token_invalidated: "舊 token 已立即失效,使用它的 MCP 設定都需更新。",
  token_copy: "複製 token",
  not_copied_title: "尚未複製 token",
  not_copied_body: "這個 token 尚未複製,關閉後無法再次查看。確定要關閉?",
  not_copied_confirm: "仍要關閉",
  not_copied_cancel: "返回",
  token_done: "已產生新的存取 token",
});

const state = { overview: null };

function fmtBytes(n) {
  const v = Number(n) || 0;
  if (v < KB) return `${v} B`;
  if (v < KB * KB) return `${(v / KB).toFixed(1)} KB`;
  return `${(v / (KB * KB)).toFixed(1)} MB`;
}

function selectText(el) {
  if (el instanceof HTMLInputElement) {
    el.focus();
    el.select();
    return;
  }
  const sel = window.getSelection();
  if (!sel) return;
  sel.removeAllRanges();
  const range = document.createRange();
  range.selectNodeContents(el);
  sel.addRange(range);
  el.focus();
}

// Clipboard API;失敗時選取文字並顯示提示。回傳是否成功寫入。
async function copyText(text, el, hint) {
  if (hint) hint.hidden = true;
  try {
    if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error("no clipboard");
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    selectText(el);
    if (hint) {
      setText(hint, T.copy_failed);
      hint.hidden = false;
    }
    return false;
  }
}

// ---- ① 總覽 ---------------------------------------------------------------------------------

function serveCommand(port) {
  return `spillway serve --port ${port}`;
}

function renderOverview(s) {
  setText(byId("ov-version"), s.version);
  setText(byId("ov-data-dir"), s.data_dir);
  setText(byId("ov-port"), s.port === null || s.port === undefined ? TEXT.dash : String(s.port));
  const status = s.token_issued ? T.issued : T.not_issued;
  setText(byId("ov-token"), status);
  setText(byId("token-status"), status);
  byId("token-status").setAttribute("data-issued", s.token_issued ? "true" : "false");
  setText(byId("serve-cmd"), s.port === null || s.port === undefined ? "spillway serve" : serveCommand(s.port));
  const c = s.context || {};
  setText(byId("ctx-stats"), T.stats(Number(c.payload_count) || 0, fmtBytes(c.payload_bytes)));
}

async function loadOverview() {
  try {
    const s = await getJSON("/api/settings");
    state.overview = s;
    renderOverview(s);
    clearMessage("overview");
    return s;
  } catch (err) {
    showError(err, { source: "overview" });
    return null;
  } finally {
    byId("overview").setAttribute("data-loaded", "true");
  }
}

// ---- ② context ------------------------------------------------------------------------------

function syncDaysNote() {
  byId("ctx-days-zero").hidden = byId("ctx-days").value.trim() !== "0";
}

function fillContext(c) {
  byId("ctx-record").checked = c.record_context_default === true;
  byId("ctx-days").value = String(c.context_retention_days);
  byId("ctx-kb").value = String(bytesToKb(c.context_max_bytes));
  syncDaysNote();
}

async function loadContext() {
  const form = byId("context-form");
  try {
    fillContext(await getJSON("/api/settings/context"));
    clearMessage("context");
  } catch (err) {
    showError(err, { source: "context" });
  } finally {
    form.setAttribute("data-loaded", "true");
  }
}

function contextError(text, el) {
  const box = byId("ctx-error");
  for (const x of byId("context-form").querySelectorAll("[aria-invalid]")) x.removeAttribute("aria-invalid");
  if (!text) {
    box.hidden = true;
    setText(box, "");
    return;
  }
  setText(box, text);
  box.hidden = false;
  if (el) {
    el.setAttribute("aria-invalid", "true");
    el.focus();
  }
}

async function saveContext(ev) {
  ev.preventDefault();
  contextError(null);
  const daysEl = byId("ctx-days");
  const kbEl = byId("ctx-kb");
  const days = Number(daysEl.value.trim());
  if (daysEl.validity.badInput || daysEl.value.trim() === "" || !Number.isInteger(days) || days < 0) {
    contextError(T.err_days, daysEl);
    return;
  }
  const kb = Number(kbEl.value.trim());
  if (kbEl.validity.badInput || kbEl.value.trim() === "" || !Number.isFinite(kb)) {
    contextError(T.err_kb, kbEl);
    return;
  }
  const body = {
    record_context_default: byId("ctx-record").checked,
    context_retention_days: days,
    context_max_bytes: kbToBytes(kb),
  };
  await withBusy(byId("ctx-save"), async () => {
    try {
      fillContext(await sendJSON("PUT", "/api/settings/context", body));
      showMessage(T.ctx_saved, "ok");
    } catch (err) {
      // 伺服端驗證(例如小於 1 KB → 422)只顯示 error.message
      contextError(err && err.message ? err.message : TEXT.http_error);
      showError(err);
    }
  });
}

// ---- ⑤ token --------------------------------------------------------------------------------

// 新 token 的一次性對話框(AM-12):
// - token 放在唯讀 <input>(方便選取),旁附 `export SPILLWAY_TOKEN=<token>`;兩者同時顯示、同時清除。
// - 「複製」用 Clipboard API,失敗時選取文字;沒按過複製就關閉(按鈕或 Esc)時再確認一次。
// - close 與 pagehide 時清除 input.value、textContent 與 JS 變數參照;token 不寫入任何瀏覽器儲存或網址。
function openTokenDialog(initialToken, { rotated }) {
  return new Promise((resolve) => {
    let token = initialToken;
    initialToken = null;
    let copied = false;
    let closing = false;
    const input = h("input", { type: "text", id: "token-value", class: "settings-token-input", readonly: true,
      autocomplete: "off", spellcheck: "false", "aria-label": T.token_label });
    input.value = token;
    const exportLine = h("pre", { class: "wrap settings-token-export", id: "token-export", tabindex: "0" },
      `export SPILLWAY_TOKEN=${token}`);
    const hint = h("p", { class: "note warn", role: "status", "data-role": "copy-hint", hidden: true });
    const copyBtn = h("button", { type: "button", class: "btn primary", "data-role": "copy" }, T.token_copy);
    const closeBtn = h("button", { type: "button", class: "btn", "data-role": "close" }, TEXT.close);
    const dialog = h("dialog", { class: "dialog", id: "token-dialog", "aria-labelledby": "token-dialog-title" },
      h("h2", { class: "dialog-title", id: "token-dialog-title" }, T.token_title),
      h("div", { class: "dialog-body" },
        h("div", { class: "row" }, h("div", { class: "grow" }, input), copyBtn),
        h("p", { class: "small" }, T.token_export),
        exportLine,
        hint,
        rotated ? h("p", { class: "warn-text" }, T.token_invalidated) : null,
        h("p", { class: "small" }, T.token_once)),
      h("div", { class: "dialog-actions" }, closeBtn));

    function wipe() {
      token = null;
      input.value = "";
      input.removeAttribute("value");
      exportLine.textContent = "";
      const sel = window.getSelection();
      if (sel) sel.removeAllRanges();
    }

    async function requestClose() {
      if (closing) return;
      if (!copied) {
        closing = true;
        const ok = await confirmDialog({ title: T.not_copied_title, body: T.not_copied_body,
          confirmLabel: T.not_copied_confirm, cancelLabel: T.not_copied_cancel });
        closing = false;
        if (!ok) return;
      }
      if (dialog.open) dialog.close();
    }

    function onPageHide() {
      wipe();
      if (dialog.open) dialog.close();
    }

    copyBtn.addEventListener("click", async () => {
      if (token === null) return;
      if (await copyText(token, input, hint)) {
        copied = true;
        showMessage(T.copied, "ok");
      }
    });
    closeBtn.addEventListener("click", requestClose);
    dialog.addEventListener("cancel", (ev) => {
      ev.preventDefault();
      requestClose();
    });
    dialog.addEventListener("close", () => {
      wipe();
      window.removeEventListener("pagehide", onPageHide);
      dialog.remove();
      resolve();
    });
    window.addEventListener("pagehide", onPageHide);
    document.body.append(dialog);
    dialog.showModal();
    closeBtn.focus();
  });
}

async function rotateToken() {
  const issued = !!(state.overview && state.overview.token_issued);
  const ok = await confirmDialog(issued
    ? { title: T.rotate_title, body: T.rotate_body, confirmLabel: T.rotate_confirm, danger: true }
    : { title: T.first_title, body: T.first_body, confirmLabel: T.first_confirm });
  if (!ok) return;
  await withBusy(byId("token-rotate"), async () => {
    let resp = null;
    try {
      resp = await sendJSON("POST", "/api/settings/token/rotate");
    } catch (err) {
      showError(err);
      return;
    }
    let token = resp && typeof resp.token === "string" ? resp.token : "";
    resp = null;
    const shown = openTokenDialog(token, { rotated: issued });
    token = null;
    await shown;
    showMessage(T.token_done, "ok");
    await loadOverview();
  });
}

// ---- 啟動 -----------------------------------------------------------------------------------

function init() {
  byId("serve-copy").addEventListener("click", async () => {
    const el = byId("serve-cmd");
    if (await copyText(el.textContent, el, byId("serve-copy-hint"))) showMessage(T.copied, "ok");
  });
  byId("ctx-days").addEventListener("input", syncDaysNote);
  byId("context-form").addEventListener("submit", saveContext);
  byId("token-rotate").addEventListener("click", rotateToken);
  mountImport({ onImported: loadOverview });
  initShell();
  loadOverview();
  loadContext();
}

init();

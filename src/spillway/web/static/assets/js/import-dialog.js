// UI-7 設定檔匯入(CHG-20261103-01 T8;決策 17 ④,決策審議修訂 AM-9、AM-13;CHG-20261102-02 決策 20)。
// - 選檔:瀏覽器先檢查 ≤ 1 MiB(超過不送出),讀入 bytes;mode 為 merge(預設)或 replace。
// - 選檔與選 mode 都不送請求;按「預覽匯入」才送一次 `POST /api/config/import?mode=<m>&dry_run=true`。
// - 預覽成功 → 確認對話框(confirmDialog:預設焦點在「取消」,Esc = 取消)顯示新增/更新/刪除計數、privileged
//   成員 id 與說明;replace 的標題與主按鈕為 danger 樣式,並顯示「會先備份到 backups/」。
//   對話框持有預覽時的 bytes 與 mode,按「匯入」送出同一份 bytes(非 dry_run)→ 顯示結果與備份檔名。
// - 檔案或 mode 改變後,先前的預覽作廢(進行中的預覽回應不再開啟對話框)。
// - dry_run 回 422/409 時直接列出錯誤(422 的全部錯誤,上限 20 條),不出現確認對話框。

import { sendText, withQuery } from "./api.js";
import { byId, clear, h } from "./dom.js";
import { TEXT } from "./strings.js";
import { confirmDialog, errorText, latest, showMessage, withBusy } from "./ui.js";

export const MAX_IMPORT_BYTES = 1_048_576;
export const MAX_ERRORS = 20;
const YAML_TYPE = "application/yaml";

const T = Object.freeze({
  too_large: `設定檔超過上限 1 MiB(${MAX_IMPORT_BYTES.toLocaleString("en-US")} bytes),未送出`,
  read_failed: "無法讀取所選的檔案",
  preview_failed: "預覽失敗(尚未寫入):",
  import_failed: "匯入失敗:",
  title: (mode) => `確認匯入(${mode})`,
  lead: "預覽結果(尚未寫入):",
  kinds: [["deployments", "成員"], ["groups", "群組"], ["routes", "route"]],
  created: "新增",
  updated: "更新",
  deleted: "刪除",
  privileged: "下列成員的憑證參照、上游網址、供應商或 CLI 參數會改變:",
  no_privileged: "沒有成員的憑證參照、上游網址、供應商或 CLI 參數會改變。",
  backup_note: "會先備份到 backups/。",
  confirm: "匯入",
  done: (mode) => `已匯入(${mode})。`,
  backup: "備份檔:",
  no_backup: "merge 不建立備份。",
  imported: "已匯入設定",
});

// 422 的訊息為「; 」串接的全部錯誤(伺服端上限 20 條);其他錯誤只有一條
export function errorLines(err) {
  const text = errorText(err);
  if (err && err.status === 422) return text.split("; ").filter(Boolean).slice(0, MAX_ERRORS);
  return [text];
}

function countsTable(result) {
  const replace = result.mode === "replace";
  const cell = (kind, key) => {
    const n = Number((result[key] || {})[kind]) || 0;
    const warn = key === "deleted" && replace && n > 0;
    return h("td", { class: warn ? "num danger-text" : "num", "data-count": `${kind}.${key}` }, String(n));
  };
  return h("table", { class: "settings-counts", "data-role": "counts" },
    h("thead", {}, h("tr", {}, h("th", {}, h("span", { class: "sr-only" }, "項目")),
      h("th", { class: "num" }, T.created), h("th", { class: "num" }, T.updated),
      h("th", { class: "num" }, T.deleted))),
    h("tbody", {}, ...T.kinds.map(([kind, name]) => h("tr", { "data-kind": kind }, h("th", {}, name),
      cell(kind, "created"), cell(kind, "updated"), cell(kind, "deleted")))));
}

function confirmBody(result) {
  const ids = Array.isArray(result.privileged) ? result.privileged : [];
  return h("div", { class: "stack", "data-role": "import-preview" },
    h("p", {}, T.lead),
    countsTable(result),
    ids.length
      ? h("p", { class: "note warn", "data-role": "privileged-note" }, T.privileged,
        h("span", { class: "mono", "data-role": "privileged" }, ids.join(", ")))
      : h("p", { class: "muted", "data-role": "privileged-note" }, T.no_privileged),
    result.mode === "replace" ? h("p", { class: "danger-text", "data-role": "backup-note" }, T.backup_note) : null);
}

export function mountImport({ onImported = null } = {}) {
  const fileInput = byId("import-file");
  const previewBtn = byId("import-preview");
  const errorsBox = byId("import-errors");
  const resultBox = byId("import-result");
  const guard = latest();
  const state = { bytes: null };

  function mode() {
    const checked = document.querySelector('input[name="import_mode"]:checked');
    return checked ? checked.value : "merge";
  }

  function clearOutput() {
    errorsBox.hidden = true;
    clear(errorsBox);
    resultBox.hidden = true;
    clear(resultBox);
  }

  function showErrors(lead, lines) {
    clear(errorsBox);
    errorsBox.append(h("p", {}, lead),
      h("ul", { class: "settings-errors", "data-role": "errors" }, ...lines.map((l) => h("li", {}, l))));
    errorsBox.hidden = false;
  }

  // 檔案或 mode 改變:先前的預覽作廢
  function invalidate() {
    guard.invalidate();
    clearOutput();
  }

  fileInput.addEventListener("change", async () => {
    invalidate();
    state.bytes = null;
    previewBtn.disabled = true;
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    if (file.size > MAX_IMPORT_BYTES) {
      showErrors(T.preview_failed, [T.too_large]);
      return;
    }
    const isLatest = guard();
    let buf;
    try {
      buf = await file.arrayBuffer();
    } catch {
      if (isLatest()) showErrors(T.preview_failed, [T.read_failed]);
      return;
    }
    if (!isLatest()) return;
    state.bytes = buf;
    previewBtn.disabled = false;
  });

  for (const radio of document.querySelectorAll('input[name="import_mode"]')) {
    radio.addEventListener("change", invalidate);
  }

  async function runImport(held) {
    try {
      const r = await sendText("POST", withQuery("/api/config/import", { mode: held.mode }), held.bytes, YAML_TYPE);
      clear(resultBox);
      resultBox.append(h("p", {}, T.done(r.mode)),
        r.backup
          ? h("p", {}, T.backup, h("span", { class: "mono", "data-role": "backup" }, `backups/${r.backup}`))
          : h("p", { "data-role": "no-backup" }, T.no_backup));
      resultBox.hidden = false;
      showMessage(T.imported, "ok");
      if (onImported) await onImported(r);
    } catch (err) {
      showErrors(T.import_failed, errorLines(err));
    }
  }

  previewBtn.addEventListener("click", async () => {
    if (!state.bytes) return;
    clearOutput();
    // 對話框持有這一次預覽的 bytes 與 mode
    const held = { bytes: state.bytes, mode: mode() };
    const isLatest = guard();
    await withBusy(previewBtn, async () => {
      let result;
      try {
        result = await sendText("POST", withQuery("/api/config/import", { mode: held.mode, dry_run: "true" }),
          held.bytes, YAML_TYPE);
      } catch (err) {
        if (isLatest()) showErrors(T.preview_failed, errorLines(err));
        return;
      }
      if (!isLatest()) return;
      const replace = held.mode === "replace";
      const ok = await confirmDialog({ title: T.title(held.mode), body: confirmBody(result),
        confirmLabel: T.confirm, cancelLabel: TEXT.cancel, danger: replace });
      if (!ok || !isLatest()) return;
      await runImport(held);
    });
    if (!state.bytes) previewBtn.disabled = true;
  });
}

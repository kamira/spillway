// UI-2 成員(CHG-20261103-01 T5;決策 12)。
// - 清單:id、類型、供應商、模型、啟用、tags、額度、冷卻;資料取自 `GET /api/deployments` 與 `GET /api/adapters`
//   (供應商的模組不可用時標「模組不可用」)。
// - `members.html#<id>` 開啟該成員的編輯表單(hashchange 亦同);「+ 新增成員」開啟空白表單。
// - 啟用切換:先 `GET /api/deployments/{id}`,改 `enabled` 後 PUT 全量(不以清單的舊資料覆寫)。
// - 測試呼叫:`POST …/test`,顯示狀態、延遲與 tokens。刪除:確認對話框列出一併刪除的項目後 DELETE。

import { getJSON, sendJSON } from "./api.js";
import { byId, clear, h, setText } from "./dom.js";
import { mountMemberForm } from "./member-form.js";
import { BUDGET_TYPE, COOLING_REASON, STATUS, TEXT, WINDOW, label } from "./strings.js";
import {
  codeLabel, confirmDialog, fmtUSD, initShell, kindBadge, setLoading, showError, showMessage, timeEl, withBusy,
} from "./ui.js";

const S = Object.freeze({
  col_id: "成員",
  col_kind: "類型",
  col_provider: "供應商",
  col_model: "模型",
  col_tags: "Tag",
  col_budget: "額度",
  col_cooling: "冷卻",
  col_enabled: "啟用",
  empty: "尚未建立成員。按「+ 新增成員」加入第一個成員。",
  unavailable: "模組不可用",
  no_budget: "不設額度",
  edit: "編輯",
  create: "新增成員",
  test: "測試呼叫",
  test_note: "會實際呼叫上游一次,並記一列帳本",
  delete: "刪除",
  unmanageable: "id 為 . 或 .. 的成員無法在畫面上管理",
  not_found: "找不到成員",
  deleted: "已刪除成員",
  toggled_on: "已啟用",
  toggled_off: "已停用",
  test_status: "狀態",
  test_latency: "延遲",
  test_tokens: "tokens",
  test_in: "輸入",
  test_out: "輸出",
  test_error: "錯誤",
  delete_title: "刪除成員",
  delete_body: "會一併刪除此成員的 tags、額度設定與所有 route 中的排序;帳本紀錄保留。",
  delete_tags: "tags",
  delete_budget: "額度",
  delete_order: "route 排序",
  delete_ledger: "帳本紀錄保留,不會刪除。",
});

const state = { members: [], modules: [], unavailable: new Set(), form: null, current: null };

function budgetText(b) {
  if (!b) return S.no_budget;
  const limit = b.limit_amount === null || b.limit_amount === undefined ? TEXT.unlimited : fmtUSD(b.limit_amount);
  return `${label(BUDGET_TYPE, b.type)} ${limit} · ${label(WINDOW, b.window)}`;
}

function memberHref(id) {
  return `#${encodeURIComponent(id)}`;
}

// id 為 . 或 .. 的成員:網址會被正規化成別的路徑,畫面無法管理(後端限制延後到 hardening CHG;審議 M5)
function unmanageable(id) {
  return id === "." || id === "..";
}

function clearHash() {
  history.replaceState(null, "", window.location.pathname + window.location.search);
}

// 供應商無法使用:模組載入失敗,或沒有任何模組處理它(cli 沒有 fallback 模組)
function providerUnavailable(d) {
  if (state.unavailable.has(d.provider)) return true;
  const mods = state.modules.filter((m) => m.available);
  if (!state.modules.length || mods.some((m) => m.providers.includes(d.provider))) return false;
  return !mods.some((m) => (m.fallback_for_kinds || []).includes(d.kind));
}

function memberRow(d) {
  const bad = unmanageable(d.id);
  const toggle = h("input", { type: "checkbox", checked: d.enabled, "aria-label": `${S.col_enabled} ${d.id}`,
    "data-toggle": d.id, disabled: bad, title: bad ? S.unmanageable : null });
  if (!bad) toggle.addEventListener("change", () => toggleEnabled(d.id, toggle));
  const provider = h("td", { class: "mono" }, d.provider);
  if (providerUnavailable(d)) {
    provider.append(" ", h("span", { class: "badge b-warn" }, S.unavailable));
  }
  const cooling = d.cooling_until
    ? h("td", {}, TEXT.cooling_until, " ", timeEl(d.cooling_until), " ",
      codeLabel(COOLING_REASON, d.cooling_reason, { class: "badge b-warn" }))
    : h("td", { class: "muted" }, "—");
  return h("tr", { "data-member": d.id, "aria-selected": state.current === d.id ? "true" : "false" },
    h("td", {}, bad ? h("span", { class: "mono", title: S.unmanageable }, d.id) : h("a", { href: memberHref(d.id) }, d.id)),
    h("td", {}, kindBadge(d.kind)),
    provider,
    h("td", { class: "mono" }, d.model),
    h("td", {}, h("span", { class: "member-tags" }, ...d.tags.map((t) => h("span", { class: "chip" }, t)))),
    h("td", { class: "nowrap" }, budgetText(d.budget)),
    cooling,
    h("td", {}, toggle));
}

function renderList() {
  const host = byId("member-table-host");
  clear(host);
  if (!state.members.length) {
    host.append(h("p", { class: "empty" }, S.empty));
    return;
  }
  host.append(h("table", {},
    h("thead", {}, h("tr", {}, ...[S.col_id, S.col_kind, S.col_provider, S.col_model, S.col_tags, S.col_budget,
      S.col_cooling, S.col_enabled].map((t) => h("th", {}, t)))),
    h("tbody", {}, ...state.members.map(memberRow))));
}

async function loadList() {
  const host = byId("member-table-host");
  if (!state.members.length) setLoading(host, true);
  try {
    const [deps, adapters] = await Promise.all([getJSON("/api/deployments"),
      getJSON("/api/adapters").catch(() => ({ modules: [] }))]);
    state.members = deps.deployments || [];
    state.modules = adapters.modules || [];
    state.unavailable = new Set(state.modules.filter((m) => !m.available).flatMap((m) => m.providers));
    renderList();
  } catch (err) {
    setLoading(host, false);
    showError(err);
  }
}

async function toggleEnabled(id, box) {
  const wanted = box.checked;
  box.disabled = true;
  try {
    const fresh = await getJSON(`/api/deployments/${encodeURIComponent(id)}`);
    const body = putBody(fresh);
    body.enabled = wanted;
    await sendJSON("PUT", `/api/deployments/${encodeURIComponent(id)}`, body);
    showMessage(`${wanted ? S.toggled_on : S.toggled_off} ${id}`, "ok");
    // 已開啟同一成員的編輯表單:同步「啟用」,之後儲存表單不會把切換蓋回去(審議 M3)
    if (state.current === id && state.form) {
      const enabledBox = state.form.element.querySelector('input[name="enabled"]');
      if (enabledBox) enabledBox.checked = wanted;
    }
    await loadList();
  } catch (err) {
    box.checked = !wanted;
    showError(err);
  } finally {
    box.disabled = false;
  }
}

function putBody(d) {
  return {
    id: d.id, model_name: d.model_name, kind: d.kind, provider: d.provider, model: d.model,
    params: d.params || {}, credential_ref: d.credential_ref ?? null, capabilities: d.capabilities || {},
    enabled: d.enabled, tags: [...(d.tags || [])], budget: d.budget ?? null,
  };
}

// ---- 編輯區 ----------------------------------------------------------------------------------

function closeEditor() {
  if (state.form) state.form.destroy();
  state.form = null;
  state.current = null;
  byId("member-editor").hidden = true;
  clearHash();
  markSelected();
}

function markSelected() {
  for (const tr of document.querySelectorAll("tr[data-member]")) {
    tr.setAttribute("aria-selected", tr.getAttribute("data-member") === state.current ? "true" : "false");
  }
}

function openEditor(member) {
  if (state.form) state.form.destroy();
  state.current = member ? member.id : null;
  const editor = byId("member-editor");
  const title = byId("editor-title");
  clear(title);
  if (member) title.append(`${S.edit} `, h("span", { class: "mono" }, member.id));
  else title.append(S.create);
  const actions = clear(byId("editor-actions"));
  const result = byId("test-result");
  result.hidden = true;
  clear(result);
  if (member) {
    const testBtn = h("button", { type: "button", class: "btn", "data-role": "test", title: S.test_note }, S.test);
    testBtn.addEventListener("click", () => testCall(member.id, testBtn));
    const delBtn = h("button", { type: "button", class: "btn", "data-role": "delete" }, S.delete);
    delBtn.addEventListener("click", () => deleteMember(member, delBtn));
    actions.append(testBtn, delBtn);
  }
  editor.hidden = false;
  state.form = mountMemberForm(byId("member-form-host"), {
    initial: member,
    onCancel: closeEditor,
    onSaved: async (saved) => {
      await loadList();
      history.replaceState(null, "", memberHref(saved.id));
      openEditor(saved);
    },
  });
  markSelected();
}

async function openFromHash() {
  const raw = window.location.hash.slice(1);
  if (!raw) return;
  let id;
  try {
    id = decodeURIComponent(raw);
  } catch {
    id = raw;
  }
  if (unmanageable(id)) {
    showMessage(`${S.unmanageable}:${id}`, "warn");
    return;
  }
  // 先比對已載入的清單:不存在的 id 只顯示訊息,不送出會 404 的請求
  if (!state.members.some((m) => m.id === id)) await loadList();
  if (!state.members.some((m) => m.id === id)) {
    showMessage(`${S.not_found}:${id}`, "warn");
    return;
  }
  try {
    const member = await getJSON(`/api/deployments/${encodeURIComponent(id)}`);
    openEditor(member);
    byId("member-editor").scrollIntoView({ block: "start" });
  } catch (err) {
    showError(err);
  }
}

async function testCall(id, btn) {
  const box = byId("test-result");
  await withBusy(btn, async () => {
    try {
      const r = await sendJSON("POST", `/api/deployments/${encodeURIComponent(id)}/test`);
      clear(box);
      box.className = `test-result ${r.ok ? "ok" : "bad"}`;
      const tokens = `${S.test_in} ${r.tokens_in ?? "—"} / ${S.test_out} ${r.tokens_out ?? "—"}`;
      box.append(
        h("div", { "data-role": "test-summary" },
          `${S.test_status}:`, codeLabel(STATUS, r.status, { "data-status": r.status }),
          ` · ${S.test_latency} ${r.latency_ms ?? "—"} ms · ${S.test_tokens} ${tokens}`),
        r.error ? h("div", { class: "mono small", "data-role": "test-error" }, `${S.test_error}:${r.error}`) : null,
        r.text ? h("div", { class: "mono small", "data-role": "test-text" }, r.text) : null);
      box.hidden = false;
      await loadList();
    } catch (err) {
      showError(err);
    }
  });
}

async function deleteMember(member, btn) {
  const body = h("div", { class: "stack tight" },
    h("p", {}, S.delete_body),
    h("ul", {},
      h("li", {}, `${S.delete_tags}:${member.tags.length ? member.tags.join(", ") : "—"}`),
      h("li", {}, `${S.delete_budget}:${budgetText(member.budget)}`),
      h("li", {}, S.delete_order)),
    h("p", { class: "muted" }, S.delete_ledger));
  const ok = await confirmDialog({ title: `${S.delete_title} ${member.id}`, body, confirmLabel: S.delete,
    danger: true });
  if (!ok) return;
  await withBusy(btn, async () => {
    try {
      await sendJSON("DELETE", `/api/deployments/${encodeURIComponent(member.id)}`);
      closeEditor();
      showMessage(`${S.deleted} ${member.id}`, "ok");
      await loadList();
    } catch (err) {
      showError(err);
    }
  });
}

// ---- 啟動 ------------------------------------------------------------------------------------

byId("new-member").addEventListener("click", () => {
  clearHash();
  openEditor(null);
});
window.addEventListener("hashchange", openFromHash);
setText(byId("editor-title"), "");
initShell();
loadList().then(openFromHash);

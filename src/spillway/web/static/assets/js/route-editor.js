// UI-4 Route 編輯頁(CHG-20261103-01 T6;決策 14,決策審議修訂 AM-1、AM-9、AM-10、AM-14、AM-17)。
// - 初始 slot 取自 GET /api/routes/{name}/effective-order(靜態順序,含停用、冷卻、未排序成員)。
// - 卡片標示:停用(/api/deployments 的 enabled)、冷卻至 <本地時間>(/api/dashboard)、未排序(unsorted)。
// - 拖曳(HTML5 DnD):卡片拖到既有 slot 或「新 slot」區;slot 以標題列整列拖曳;空 slot 自動移除。
//   拖曳目標(各 slot 與新 slot 區)在 dragstart 之前就可見且有尺寸。
//   鍵盤替代:每張卡片的「移到…」選單、每個 slot 的上移/下移按鈕。
// - 狀態轉換只經純函式 applyMove(slots, move)(匯出,測試以 page.evaluate 逐案驗證)。
// - 未儲存的變更:比較基準為載入時的 slot 結構(含 effort、weight)與 route 設定;頂端標示並以
//   beforeunload 提示。
// - 儲存:PUT …/order 寫入全部卡片(含停用、冷卻、未排序);slot 為空時等同還原預設,走同一個確認對話框;
//   之後重新 GET effective-order。

import { getJSON, sendJSON } from "./api.js";
import { byId, clear, h, setText } from "./dom.js";
import { TEXT } from "./strings.js";
import {
  clearMessage, confirmDialog, fmtLocal, fmtUSD, initShell, kindBadge, setLoading, showError,
  showMessage, withBusy,
} from "./ui.js";

// ---- 頁面字串 ---------------------------------------------------------------------------------

export const T = Object.freeze({
  missing_name: "缺少 route 名稱;請從 Route 清單開啟編輯頁。",
  restore: "還原預設順序",
  dry_run: "Dry-run",
  save: "儲存",
  dirty: "有未儲存的變更",
  dirty_hint: "離開頁面前會提示。",
  saved: "已儲存",
  no_changes: "沒有未儲存的變更",
  restored: "已還原預設順序",
  restore_title: "還原預設順序",
  restore_body: "刪除這條 route 的手動順序,改用預設順序(訂閱 → API → local)。未儲存的變更會一併捨棄。",
  restore_empty_body: "畫面上已沒有任何卡片,儲存等同還原預設順序:刪除這條 route 的手動順序。",
  restore_confirm: "還原預設",
  target: "目標",
  group: "群組",
  mode: "模式",
  route_tags: "Route tag",
  route_tags_hint: "以逗號或空白分隔",
  precheck: "預估額度檢查",
  precheck_hint: "勾選:P90 預估超過剩餘即事先跳過。不勾選:用完才跳過,下次可用時自動恢復。",
  record: "記錄 context",
  record_inherit: "依全域預設",
  record_on: "開",
  record_off: "關",
  source: "順序來源",
  manual: "手動",
  default: "預設",
  manual_note: "手動排序優先於預設的 訂閱 → API → local",
  default_note: "尚未手動排序,依預設的 訂閱 → API → local",
  chat_note: "chat route 不使用 CLI 成員",
  weight_note: "權重:高 3/中 2/低 1",
  weight_formula: "選中機率 ∝ 權重值 × 剩餘額度比例",
  weight_h: "權重怎麼算",
  slots_h: "使用順位",
  slots_hint: "拖曳卡片或 slot 標題列調整順位;也可用卡片的「移到…」選單與 slot 的上移、下移按鈕",
  slot: "Slot",
  slot_first: "先嘗試",
  slot_next: "前面的 slot 皆不可用時",
  up: "上移",
  down: "下移",
  drag_slot: "拖曳",
  new_slot: "拖曳成員到這裡,建立新的 slot",
  empty: "這條 route 的目標目前沒有任何成員。",
  effort: "effort",
  effort_none: "不指定",
  effort_extra: "(不在能力清單)",
  weight: "權重",
  move_to: "移到…",
  move_new: "新 slot",
  disabled: "停用",
  cooling: "冷卻至",
  unsorted: "未排序",
  remaining: "剩餘",
  dry_h: "Dry-run 結果",
  dry_hint: "以範例訊息模擬已儲存的設定,不呼叫上游、不寫帳本。",
  dry_random: "slot 內依權重隨機抽選,每次順序可能不同",
  dry_sample: "這是一則 dry-run 範例訊息。",
  dry_first: "第一順位",
  dry_excluded: "排除",
  dry_none: "沒有候選成員",
  dry_unsorted: "(未排序)",
});

const WEIGHTS = Object.freeze([["high", "高"], ["medium", "中"], ["low", "低"]]);

// 排除原因(routing EXCLUDE_REASONS)→ 繁中;原值放在 title
const EXCLUDE_REASON = Object.freeze({
  disabled: "停用",
  mode_chat_excludes_cli: "chat route 不使用 CLI 成員",
  ctx_exceeded: "超過 context 上限",
  tools_unsupported: "不支援 tools",
  vision_unsupported: "不支援圖片",
  adapter_unavailable: "模組不可用",
  cooling: "冷卻中",
  concurrency_full: "同時執行數已滿",
  quota_exhausted: "額度用完",
  unhealthy: "健康檢查失敗",
  skipped_precheck: "預估超過剩餘額度",
  shutting_down: "停機中",
});

// ---- 純函式:拖曳與鍵盤操作的狀態轉換 ---------------------------------------------------------

// slots:[{entries: [{deployment, effort, weight, unsorted}]}](畫面順序 = slot 順序)。
// move:
//   {kind: "card", deployment, to: <slot 索引> | "new", index?}  卡片移到該 slot(index 省略 = 末端)或新 slot(末端)
//   {kind: "slot", from, to}   slot `from` 放到原索引 `to` 之前(to = slots.length 表示最後)
//   {kind: "up", index} / {kind: "down", index}   slot 上移/下移一位
//   {kind: "set", deployment, effort?, weight?}   改 effort(null = 不指定)或 weight
// 回傳新的陣列(不改動輸入);移動後空的 slot 自動移除;被移動的卡片清除「未排序」;無效的移動回傳等值副本。
export function applyMove(slots, move) {
  const next = (slots || []).map((s) => ({ ...s, entries: (s.entries || []).map((e) => ({ ...e })) }));
  const n = next.length;
  const isIndex = (i, max) => Number.isInteger(i) && i >= 0 && i < max;
  switch (move && move.kind) {
    case "card": {
      let src = -1;
      let pos = -1;
      next.forEach((s, i) => {
        const j = s.entries.findIndex((e) => e.deployment === move.deployment);
        if (j >= 0 && src < 0) { src = i; pos = j; }
      });
      if (src < 0) return next;
      if (move.to !== "new" && !isIndex(move.to, n)) return next;
      const [card] = next[src].entries.splice(pos, 1);
      card.unsorted = false;
      if (move.to === "new") {
        next.push({ entries: [card] });
      } else {
        const dst = next[move.to].entries;
        const at = Number.isInteger(move.index) ? Math.max(0, Math.min(move.index, dst.length)) : dst.length;
        dst.splice(at, 0, card);
      }
      return next.filter((s) => s.entries.length > 0);
    }
    case "slot": {
      const { from, to } = move;
      if (!isIndex(from, n) || !Number.isInteger(to) || to < 0 || to > n) return next;
      const [slot] = next.splice(from, 1);
      next.splice(to > from ? to - 1 : to, 0, slot);
      return next.filter((s) => s.entries.length > 0);
    }
    case "up":
      return isIndex(move.index, n) && move.index > 0
        ? applyMove(next, { kind: "slot", from: move.index, to: move.index - 1 }) : next;
    case "down":
      return isIndex(move.index, n) && move.index < n - 1
        ? applyMove(next, { kind: "slot", from: move.index, to: move.index + 2 }) : next;
    case "set": {
      for (const s of next) {
        for (const e of s.entries) {
          if (e.deployment !== move.deployment) continue;
          if ("effort" in move) e.effort = move.effort || null;
          if ("weight" in move) e.weight = move.weight;
        }
      }
      return next;
    }
    default:
      throw new TypeError(`未知的 move:${move && move.kind}`);
  }
}

// 比較基準(含 effort 與 weight;不含 unsorted 標示)
export function orderKey(slots) {
  return JSON.stringify((slots || []).map((s) => s.entries.map((e) => [e.deployment, e.effort ?? null, e.weight])));
}

// PUT …/order 的 body
export function orderBody(slots) {
  return { slots: slots.map((s) => s.entries.map((e) => ({ deployment: e.deployment, effort: e.effort ?? null, weight: e.weight }))) };
}

export function parseTags(text) {
  const out = [];
  for (const t of String(text || "").split(/[,\s]+/)) {
    if (t && !out.includes(t)) out.push(t);
  }
  return out;
}

function settingsKey(s) {
  return JSON.stringify([s.mode, s.tags, s.record_context, s.budget_precheck]);
}

// ---- 頁面狀態 ---------------------------------------------------------------------------------

const state = {
  name: null,
  route: null,          // GET /api/routes/{name}
  ordered: "default",
  deps: new Map(),      // id → /api/deployments 列
  dash: new Map(),      // id → /api/dashboard 列
  slots: [],
  baseOrder: "[]",
  settings: null,       // {mode, tags, record_context, budget_precheck}
  baseSettings: "",
  drag: null,           // {kind: "card", deployment} | {kind: "slot", index}
};

function orderDirty() {
  return orderKey(state.slots) !== state.baseOrder;
}

function settingsDirty() {
  return state.settings !== null && settingsKey(state.settings) !== state.baseSettings;
}

function isDirty() {
  return orderDirty() || settingsDirty();
}

function routePath(suffix = "") {
  return `/api/routes/${encodeURIComponent(state.name)}${suffix}`;
}

async function load() {
  const [route, eff, deps, dash] = await Promise.all([
    getJSON(routePath()),
    getJSON(routePath("/effective-order")),
    getJSON("/api/deployments"),
    getJSON("/api/dashboard"),
  ]);
  state.route = route;
  state.ordered = eff.ordered;
  state.deps = new Map(deps.deployments.map((d) => [d.id, d]));
  state.dash = new Map(dash.deployments.map((d) => [d.id, d]));
  state.slots = eff.slots.map((s) => ({
    entries: s.entries.map((e) => ({ deployment: e.deployment, effort: e.effort ?? null, weight: e.weight, unsorted: !!e.unsorted })),
  }));
  state.baseOrder = orderKey(state.slots);
  state.settings = { mode: route.mode, tags: [...route.tags], record_context: route.record_context,
    budget_precheck: !!route.budget_precheck };
  state.baseSettings = settingsKey(state.settings);
}

// ---- 繪製 -------------------------------------------------------------------------------------

function update(move) {
  state.slots = applyMove(state.slots, move);
  renderSlots();
  renderDirty();
}

function renderDirty() {
  const banner = byId("dirty-banner");
  if (banner) banner.hidden = !isDirty();
}

function targetText(target) {
  if (target && typeof target.group === "string") {
    return [T.group + " ", h("a", { href: "/ui/groups.html", class: "mono" }, target.group)];
  }
  const sel = (target && target.tags) || {};
  const parts = [];
  for (const k of ["all", "any", "none"]) {
    if (sel[k] && sel[k].length) parts.push(`${k}: ${sel[k].join(", ")}`);
  }
  return [h("span", { class: "mono" }, parts.length ? `tags ${parts.join(";")}` : "tags(全部成員)")];
}

function field(labelText, control, hint) {
  return h("label", { class: "f" }, h("span", { class: "label" }, labelText), control,
    hint ? h("span", { class: "hint" }, hint) : null);
}

function renderSettings() {
  const s = state.settings;
  const mode = h("select", { id: "route-mode" },
    h("option", { value: "chat" }, "chat"), h("option", { value: "task" }, "task"));
  mode.value = s.mode;
  mode.addEventListener("change", () => { s.mode = mode.value; renderModeNote(); renderDirty(); });

  const tags = h("input", { type: "text", id: "route-tags", autocomplete: "off" });
  tags.value = s.tags.join(", ");
  tags.addEventListener("input", () => { s.tags = parseTags(tags.value); renderDirty(); });

  const pre = h("input", { type: "checkbox", id: "route-precheck" });
  pre.checked = s.budget_precheck;
  pre.addEventListener("change", () => { s.budget_precheck = pre.checked; renderDirty(); });

  const rec = h("select", { id: "route-record" },
    h("option", { value: "inherit" }, T.record_inherit),
    h("option", { value: "on" }, T.record_on),
    h("option", { value: "off" }, T.record_off));
  rec.value = s.record_context === null ? "inherit" : (s.record_context ? "on" : "off");
  rec.addEventListener("change", () => {
    s.record_context = { inherit: null, on: true, off: false }[rec.value];
    renderDirty();
  });

  const manual = state.ordered === "manual";
  return h("section", { class: "card", "aria-label": "Route 設定" },
    h("div", { class: "route-settings" },
      h("div", { class: "f" }, h("span", { class: "label" }, T.target), h("span", { id: "route-target" }, ...targetText(state.route.target))),
      field(T.mode, mode),
      field(T.route_tags, tags, T.route_tags_hint),
      h("div", { class: "f" }, h("span", { class: "label" }, "額度"),
        h("label", { class: "check" }, pre, T.precheck), h("span", { class: "hint" }, T.precheck_hint)),
      field(T.record, rec),
      h("div", { class: "f" }, h("span", { class: "label" }, T.source),
        h("span", { id: "order-source", "data-ordered": state.ordered },
          h("strong", {}, manual ? T.manual : T.default)),
        h("span", { class: "hint" }, manual ? T.manual_note : T.default_note))),
    h("p", { id: "chat-note", class: "warn-text", hidden: s.mode !== "chat" }, T.chat_note));
}

function renderModeNote() {
  const note = byId("chat-note");
  if (note) note.hidden = state.settings.mode !== "chat";
}

function remainingText(id) {
  const row = state.dash.get(id);
  if (!row) return "";
  if (row.unlimited) return `${T.remaining} ${TEXT.unlimited}`;
  return `${T.remaining} ${fmtUSD(row.remaining)}`;
}

function clearOver() {
  for (const el of document.querySelectorAll(".over, .dragging")) el.classList.remove("over", "dragging");
}

function endDrag() {
  state.drag = null;
  clearOver();
}

// 拖曳目標:slot 與新 slot 區(一直顯示,dragstart 之前就有尺寸)
function makeDropTarget(el, onDrop) {
  el.addEventListener("dragover", (e) => {
    if (!state.drag) return;
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
    el.classList.add("over");
  });
  el.addEventListener("dragleave", (e) => {
    if (!el.contains(e.relatedTarget)) el.classList.remove("over");
  });
  el.addEventListener("drop", (e) => {
    if (!state.drag) return;
    e.preventDefault();
    e.stopPropagation();
    const drag = state.drag;
    endDrag();
    onDrop(drag);
  });
}

function makeDragSource(el, payload, moving) {
  el.setAttribute("draggable", "true");
  el.addEventListener("dragstart", (e) => {
    e.stopPropagation();
    state.drag = payload;
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", payload.kind === "card" ? payload.deployment : `slot:${payload.index}`);
    }
    moving.classList.add("dragging");
  });
  el.addEventListener("dragend", endDrag);
}

function effortSelect(entry, dep) {
  const efforts = (dep && dep.capabilities && Array.isArray(dep.capabilities.efforts)) ? dep.capabilities.efforts : [];
  const sel = h("select", { "data-role": "effort", "aria-label": `${entry.deployment} effort` },
    h("option", { value: "" }, T.effort_none),
    ...efforts.map((x) => h("option", { value: x }, x)));
  // 已存的 effort 不在能力清單時保留原值(避免儲存時被悄悄改掉)
  if (entry.effort && !efforts.includes(entry.effort)) {
    sel.append(h("option", { value: entry.effort }, `${entry.effort}${T.effort_extra}`));
  }
  sel.value = entry.effort || "";
  sel.addEventListener("change", () => update({ kind: "set", deployment: entry.deployment, effort: sel.value || null }));
  return sel;
}

function weightSelect(entry) {
  const sel = h("select", { "data-role": "weight", "aria-label": `${entry.deployment} ${T.weight}` },
    ...WEIGHTS.map(([v, l]) => h("option", { value: v }, l)));
  sel.value = entry.weight;
  sel.addEventListener("change", () => update({ kind: "set", deployment: entry.deployment, weight: sel.value }));
  return sel;
}

function moveSelect(entry, slotIndex) {
  const sel = h("select", { "data-role": "move", "aria-label": `${entry.deployment} ${T.move_to}` },
    h("option", { value: "" }, T.move_to),
    ...state.slots.map((_, j) => h("option", { value: String(j), disabled: j === slotIndex }, `${T.slot} ${j + 1}`)),
    h("option", { value: "new" }, T.move_new));
  sel.value = "";
  sel.addEventListener("change", () => {
    if (!sel.value) return;
    update({ kind: "card", deployment: entry.deployment, to: sel.value === "new" ? "new" : Number(sel.value) });
  });
  return sel;
}

function renderCard(entry, slotIndex) {
  const dep = state.deps.get(entry.deployment);
  const kind = dep ? dep.kind : (state.dash.get(entry.deployment) || {}).kind;
  const row = state.dash.get(entry.deployment);
  const marks = [];
  if (dep && dep.enabled === false) marks.push(h("span", { class: "badge b-muted", "data-mark": "disabled" }, T.disabled));
  if (row && row.cooling_until) {
    marks.push(h("span", { class: "badge b-warn", "data-mark": "cooling", title: row.cooling_until },
      `${T.cooling} ${fmtLocal(row.cooling_until)}`));
  }
  if (entry.unsorted) marks.push(h("span", { class: "badge b-warn", "data-mark": "unsorted" }, T.unsorted));

  const head = h("div", { class: "member-head" },
    h("span", { class: "handle", "aria-hidden": "true" }, "⋮⋮"),
    h("span", { class: "mono" }, entry.deployment),
    kindBadge(kind),
    h("span", { class: "muted small", "data-role": "remaining" }, remainingText(entry.deployment)),
    ...marks);
  const card = h("div", { class: "member", "data-deployment": entry.deployment },
    head,
    h("div", { class: "member-controls" },
      h("label", {}, T.effort, effortSelect(entry, dep)),
      h("label", {}, T.weight, weightSelect(entry)),
      moveSelect(entry, slotIndex)));
  makeDragSource(head, { kind: "card", deployment: entry.deployment }, card);
  return card;
}

function renderSlot(slot, i) {
  const n = state.slots.length;
  const up = h("button", { type: "button", class: "btn sm", "data-role": "up", "aria-label": `${T.slot} ${i + 1} ${T.up}`, disabled: i === 0 }, T.up);
  const down = h("button", { type: "button", class: "btn sm", "data-role": "down", "aria-label": `${T.slot} ${i + 1} ${T.down}`, disabled: i === n - 1 }, T.down);
  up.addEventListener("click", () => update({ kind: "up", index: i }));
  down.addEventListener("click", () => update({ kind: "down", index: i }));
  const kinds = [...new Set(slot.entries.map((e) => (state.deps.get(e.deployment) || {}).kind).filter(Boolean))];
  const head = h("div", { class: "slothead", "aria-label": `${T.drag_slot} ${T.slot} ${i + 1}` },
    h("span", { class: "handle", "aria-hidden": "true" }, "⋮⋮"),
    h("h3", {}, `${T.slot} ${i + 1}`),
    h("span", { class: "muted small" }, i === 0 ? T.slot_first : T.slot_next),
    h("span", { class: "slot-summary" }, ...kinds.map((k) => kindBadge(k))),
    h("span", { class: "slot-tools" }, up, down));
  const el = h("section", { class: "slot", "data-slot-index": String(i), "aria-label": `${T.slot} ${i + 1}` },
    head,
    h("div", { class: "slot-cards" }, ...slot.entries.map((e) => renderCard(e, i))));
  makeDragSource(head, { kind: "slot", index: i }, el);
  makeDropTarget(el, (drag) => {
    if (drag.kind === "card") {
      update({ kind: "card", deployment: drag.deployment, to: i });
    } else if (drag.index !== i) {
      // 往上拖:放到此 slot 之前;往下拖:放到此 slot 之後
      update({ kind: "slot", from: drag.index, to: drag.index > i ? i : i + 1 });
    }
  });
  return el;
}

function renderSlots() {
  const list = byId("slots");
  if (!list) return;
  clear(list);
  if (state.slots.length === 0) list.append(h("p", { class: "empty" }, T.empty));
  state.slots.forEach((s, i) => list.append(renderSlot(s, i)));
  list.setAttribute("data-count", String(state.slots.length));
}

function renderEditor() {
  const editor = byId("editor");
  clear(editor);
  const newZone = h("div", { class: "drop", id: "new-slot", "aria-label": T.move_new }, T.new_slot);
  makeDropTarget(newZone, (drag) => {
    if (drag.kind === "card") update({ kind: "card", deployment: drag.deployment, to: "new" });
    else update({ kind: "slot", from: drag.index, to: state.slots.length });
  });
  editor.append(
    h("div", { id: "dirty-banner", class: "dirty-banner", role: "status", hidden: true },
      h("strong", {}, T.dirty), h("span", {}, T.dirty_hint)),
    renderSettings(),
    h("div", { class: "route-layout" },
      h("section", { "aria-labelledby": "slots-h", class: "stack" },
        h("div", { class: "row" }, h("h2", { id: "slots-h" }, T.slots_h), h("span", { class: "muted small" }, T.slots_hint)),
        h("div", { id: "slots", class: "slot-list" }),
        newZone),
      h("aside", { class: "stack" },
        h("section", { class: "card", "aria-labelledby": "dry-h", id: "dry-panel" },
          h("h2", { id: "dry-h" }, T.dry_h),
          h("p", { class: "muted small" }, T.dry_hint),
          h("p", { class: "muted small", id: "dry-random" }, T.dry_random),
          h("div", { id: "dry-result" })),
        h("section", { class: "card", "aria-labelledby": "w-h" },
          h("h2", { id: "w-h" }, T.weight_h),
          h("p", { id: "weight-note" }, T.weight_note),
          h("div", { class: "weight-scale" },
            h("div", {}, h("div", {}, "高"), h("div", { class: "muted" }, "3")),
            h("div", {}, h("div", {}, "中"), h("div", { class: "muted" }, "2")),
            h("div", {}, h("div", {}, "低"), h("div", { class: "muted" }, "1"))),
          h("p", { class: "muted small" }, T.weight_formula)))));
  renderSlots();
  renderDirty();
  editor.setAttribute("data-state", "ready");
}

// ---- 動作 -------------------------------------------------------------------------------------

async function reload() {
  const editor = byId("editor");
  editor.setAttribute("data-state", "loading");
  await load();
  renderEditor();
}

async function restoreDefault(body) {
  const ok = await confirmDialog({ title: T.restore_title, body, confirmLabel: T.restore_confirm, danger: true });
  if (!ok) return false;
  await sendJSON("DELETE", routePath("/order"));
  return true;
}

async function onRestore() {
  try {
    if (await restoreDefault(T.restore_body)) {
      await reload();
      showMessage(T.restored, "ok");
    }
  } catch (err) {
    showError(err);
  }
}

async function onSave() {
  clearMessage();
  const empty = state.slots.length === 0;
  if (!isDirty() && !(empty && state.ordered === "manual")) {
    showMessage(T.no_changes, "info");
    return;
  }
  try {
    if (settingsDirty()) {
      const s = state.settings;
      await sendJSON("PUT", routePath(), { target: state.route.target, mode: s.mode, tags: s.tags,
        record_context: s.record_context, budget_precheck: s.budget_precheck });
      state.baseSettings = settingsKey(s);
    }
    if (empty) {
      // 把所有卡片拖出(slots = [])等同還原預設,走同一個確認對話框
      if (orderDirty() || state.ordered === "manual") {
        if (!(await restoreDefault(T.restore_empty_body))) {
          renderDirty();
          return;
        }
      }
    } else if (orderDirty()) {
      await sendJSON("PUT", routePath("/order"), orderBody(state.slots));
    }
    await reload();
    showMessage(T.saved, "ok");
  } catch (err) {
    renderDirty();
    showError(err);
  }
}

function excludedLabel(code) {
  return h("span", { title: code }, Object.prototype.hasOwnProperty.call(EXCLUDE_REASON, code) ? EXCLUDE_REASON[code] : code);
}

async function onDryRun() {
  const out = byId("dry-result");
  setLoading(out, true);
  try {
    const plan = await sendJSON("POST", routePath("/dry-run"), { messages: [{ role: "user", content: T.dry_sample }] });
    setLoading(out, false);
    clear(out);
    const items = plan.slots.map((s) => h("li", { "data-slot": String(s.number ?? "") },
      h("strong", {}, `${T.slot} ${s.number ?? ""}`), " ",
      ...s.entries.map((e, k) => h("span", { class: "dry-entry" }, k ? "、" : "",
        h("span", { class: "mono", "data-deployment": e.deployment }, e.deployment), " ", kindBadge(e.kind),
        e.unsorted ? T.dry_unsorted : ""))));
    out.append(
      h("p", {}, `${T.dry_first}:`, h("span", { class: "mono", id: "dry-first" }, plan.first || T.dry_none)),
      items.length ? h("ol", { class: "dry-list", id: "dry-slots" }, ...items) : h("p", { class: "muted" }, T.dry_none));
    if (plan.excluded.length) {
      out.append(h("p", { class: "muted small" }, T.dry_excluded),
        h("ul", { class: "plain", id: "dry-excluded" }, ...plan.excluded.map((x) => h("li", { "data-deployment": x.deployment },
          h("span", { class: "mono" }, x.deployment), excludedLabel(x.reason)))));
    }
  } catch (err) {
    setLoading(out, false);
    showError(err);
  }
}

// ---- 進入點 -----------------------------------------------------------------------------------

function renderActions() {
  const actions = byId("route-actions");
  const restore = h("button", { type: "button", class: "btn", id: "btn-restore" }, T.restore);
  const dry = h("button", { type: "button", class: "btn", id: "btn-dry-run" }, T.dry_run);
  const save = h("button", { type: "button", class: "btn primary", id: "btn-save" }, T.save);
  restore.addEventListener("click", () => withBusy(restore, onRestore));
  dry.addEventListener("click", () => withBusy(dry, onDryRun));
  save.addEventListener("click", () => withBusy(save, onSave));
  actions.append(restore, dry, save);
}

async function main() {
  initShell();
  const editor = byId("editor");
  if (!editor) return;
  state.name = new URLSearchParams(window.location.search).get("name");
  if (!state.name) {
    showMessage(T.missing_name, "warn");
    editor.setAttribute("data-state", "missing");
    return;
  }
  setText(byId("route-name"), state.name);
  window.addEventListener("beforeunload", (e) => {
    if (!isDirty()) return;
    e.preventDefault();
    e.returnValue = "";
  });
  setLoading(editor, true);
  try {
    await load();
  } catch (err) {
    setLoading(editor, false);
    editor.setAttribute("data-state", "error");
    showError(err);
    return;
  }
  renderActions();
  renderEditor();
}

main();

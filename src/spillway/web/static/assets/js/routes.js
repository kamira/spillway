// UI-4 Route 清單(CHG-20261103-01 T6;決策 14、設計差異表 #6)。
// 清單:名稱(連到編輯頁)、mode、target、route tags、預估檢查、記錄 context;新增、刪除(確認)。
// 「記錄 context」三態:依全域預設 / 開 / 關 ↔ null / true / false。

import { getJSON, sendJSON } from "./api.js";
import { byId, clear, h } from "./dom.js";
import { confirmDialog, initShell, setLoading, showError, showMessage, withBusy } from "./ui.js";

export const T = Object.freeze({
  name: "名稱",
  mode: "模式",
  target: "目標",
  tags: "Route tag",
  precheck: "預估檢查",
  record: "記錄 context",
  actions: "操作",
  edit: "編輯",
  del: "刪除",
  yes: "是",
  no: "否",
  record_inherit: "依全域預設",
  record_on: "開",
  record_off: "關",
  empty: "尚未建立任何 route。",
  group: "群組",
  target_kind: "目標類型",
  target_group: "群組",
  target_tags: "tags 選擇器",
  no_groups: "(尚無群組)",
  all: "全部符合(all)",
  any: "任一符合(any)",
  none: "排除(none)",
  sep_hint: "以逗號或空白分隔",
  route_tags_hint: "route 本身的標籤,以逗號或空白分隔",
  precheck_label: "預估額度檢查",
  create: "新增 route",
  created: "已新增 route",
  deleted: "已刪除 route",
  delete_title: "刪除 route",
  delete_body: "會一併刪除這條 route 的手動順序;帳本保留。此操作無法復原。",
  name_required: "請填寫 route 名稱",
  group_required: "請選擇群組",
});

export function parseList(text) {
  const out = [];
  for (const t of String(text || "").split(/[,\s]+/)) {
    if (t && !out.includes(t)) out.push(t);
  }
  return out;
}

export function recordLabel(v) {
  if (v === null || v === undefined) return T.record_inherit;
  return v ? T.record_on : T.record_off;
}

export function recordValue(choice) {
  return { inherit: null, on: true, off: false }[choice] ?? null;
}

export function targetSummary(target) {
  if (target && typeof target.group === "string") return `${T.group} ${target.group}`;
  const sel = (target && target.tags) || {};
  const parts = ["all", "any", "none"].filter((k) => sel[k] && sel[k].length).map((k) => `${k}: ${sel[k].join(", ")}`);
  return parts.length ? `tags ${parts.join(";")}` : "tags(全部成員)";
}

function editHref(name) {
  return `/ui/route.html?name=${encodeURIComponent(name)}`;
}

async function onDelete(name) {
  const ok = await confirmDialog({ title: `${T.delete_title} ${name}`, body: T.delete_body, confirmLabel: T.del, danger: true });
  if (!ok) return;
  try {
    await sendJSON("DELETE", `/api/routes/${encodeURIComponent(name)}`);
    showMessage(`${T.deleted} ${name}`, "ok");
    await loadList();
  } catch (err) {
    showError(err);
  }
}

function renderList(routes) {
  const box = byId("routes-list");
  clear(box);
  box.removeAttribute("aria-busy");
  if (!routes.length) {
    box.append(h("p", { class: "empty", id: "routes-empty" }, T.empty));
    return;
  }
  const rows = routes.map((r) => {
    const del = h("button", { type: "button", class: "btn sm danger", "data-role": "delete", "aria-label": `${T.del} ${r.name}` }, T.del);
    del.addEventListener("click", () => withBusy(del, () => onDelete(r.name)));
    return h("tr", { "data-route": r.name },
      h("td", {}, h("a", { href: editHref(r.name), class: "mono" }, r.name)),
      h("td", { "data-col": "mode" }, r.mode),
      h("td", { "data-col": "target", class: "mono small" }, targetSummary(r.target)),
      h("td", { "data-col": "tags" }, h("span", { class: "route-tags" }, ...r.tags.map((t) => h("span", { class: "chip" }, t)))),
      h("td", { "data-col": "precheck" }, r.budget_precheck ? T.yes : T.no),
      h("td", { "data-col": "record" }, recordLabel(r.record_context)),
      h("td", { class: "nowrap" }, h("a", { href: editHref(r.name), class: "btn sm" }, T.edit), " ", del));
  });
  box.append(h("table", { id: "routes-table" },
    h("thead", {}, h("tr", {}, ...[T.name, T.mode, T.target, T.tags, T.precheck, T.record, T.actions].map((c) => h("th", {}, c)))),
    h("tbody", {}, ...rows)));
}

async function loadList() {
  const box = byId("routes-list");
  setLoading(box, true);
  try {
    const data = await getJSON("/api/routes");
    renderList(data.routes);
  } catch (err) {
    setLoading(box, false);
    showError(err);
  }
}

function input(id, attrs = {}) {
  return h("input", { type: "text", id, name: id, autocomplete: "off", ...attrs });
}

function fieldEl(labelText, control, hint, attrs = {}) {
  return h("label", { class: "f", ...attrs }, labelText, control, hint ? h("span", { class: "hint" }, hint) : null);
}

async function renderForm() {
  const form = byId("route-new");
  clear(form);
  let groups = [];
  try {
    groups = (await getJSON("/api/groups")).groups.map((g) => g.name);
  } catch (err) {
    showError(err);
  }
  const name = input("new-name", { required: true, maxlength: "200" });
  const mode = h("select", { id: "new-mode" }, h("option", { value: "chat" }, "chat"), h("option", { value: "task" }, "task"));
  const kind = h("select", { id: "new-target-kind" },
    h("option", { value: "group" }, T.target_group), h("option", { value: "tags" }, T.target_tags));
  const group = h("select", { id: "new-group" },
    ...(groups.length ? groups.map((g) => h("option", { value: g }, g)) : [h("option", { value: "" }, T.no_groups)]));
  const all = input("new-all");
  const any = input("new-any");
  const none = input("new-none");
  const tags = input("new-tags");
  const pre = h("input", { type: "checkbox", id: "new-precheck" });
  const rec = h("select", { id: "new-record" },
    h("option", { value: "inherit" }, T.record_inherit), h("option", { value: "on" }, T.record_on),
    h("option", { value: "off" }, T.record_off));
  kind.value = groups.length ? "group" : "tags";

  const groupField = fieldEl(T.target_group, group);
  const tagFields = h("div", { class: "route-form-grid" },
    fieldEl(T.all, all, T.sep_hint), fieldEl(T.any, any, T.sep_hint), fieldEl(T.none, none, T.sep_hint));
  const sync = () => {
    groupField.hidden = kind.value !== "group";
    tagFields.hidden = kind.value !== "tags";
  };
  kind.addEventListener("change", sync);
  sync();

  const submit = h("button", { type: "submit", class: "btn primary", id: "new-submit" }, T.create);
  form.append(
    h("div", { class: "route-form-grid" },
      fieldEl(T.name, name), fieldEl(T.mode, mode), fieldEl(T.target_kind, kind), groupField),
    tagFields,
    h("div", { class: "route-form-grid" },
      fieldEl(T.tags, tags, T.route_tags_hint),
      fieldEl(T.record, rec),
      h("label", { class: "check" }, pre, T.precheck_label)),
    h("div", { class: "row end" }, submit));

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    withBusy(submit, async () => {
      const routeName = name.value.trim();
      if (!routeName) {
        showMessage(T.name_required, "error");
        return;
      }
      let target;
      if (kind.value === "group") {
        if (!group.value) {
          showMessage(T.group_required, "error");
          return;
        }
        target = { group: group.value };
      } else {
        target = { tags: { all: parseList(all.value), any: parseList(any.value), none: parseList(none.value) } };
      }
      const body = { name: routeName, mode: mode.value, target, tags: parseList(tags.value),
        record_context: recordValue(rec.value), budget_precheck: pre.checked };
      try {
        await sendJSON("POST", "/api/routes", body);
        showMessage(`${T.created} ${routeName}`, "ok");
        name.value = "";
        await loadList();
      } catch (err) {
        showError(err);
      }
    });
  });
  form.setAttribute("data-state", "ready");
}

async function main() {
  initShell();
  if (!byId("routes-list")) return;
  await Promise.all([loadList(), renderForm()]);
}

main();

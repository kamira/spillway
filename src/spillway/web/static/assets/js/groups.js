// UI-3 群組與 Tag(CHG-20261103-01 T5;決策 13、24)。
// - 群組清單(名稱、符合的成員數);編輯器以 all / any / none 三列 tag 輸入(Enter 加入、× 移除),
//   每次變更後 300 ms 以 `POST /api/groups/preview` 即時預覽符合的成員。
// - 新增(POST)名稱違反規則時顯示 422 訊息;刪除被 route 引用的群組時顯示 409 訊息(含 route 名)。
// - 「所有 Tag」:列出全部 tag 與各 tag 的成員;點成員開啟 UI-2 編輯表單(members.html#<id>)。
//   tag 改名不做(決策 24)。

import { getJSON, sendJSON } from "./api.js";
import { byId, clear, h, setText } from "./dom.js";
import { confirmDialog, initShell, kindBadge, setLoading, showError, showMessage, withBusy } from "./ui.js";

const PREVIEW_DELAY_MS = 300;
const ROWS = [
  ["all", "全部包含"],
  ["any", "任一包含"],
  ["none", "排除"],
];

const S = Object.freeze({
  members_count: (n) => `${n} 個成員`,
  empty_groups: "尚未建立群組",
  new_group: "新增群組",
  edit_group: "群組條件",
  name: "名稱",
  tag_placeholder: "輸入 tag 後按 Enter",
  tag_add: "+ tag",
  tag_remove: "移除",
  unset: "未設定",
  preview: (n) => `符合的成員(${n})`,
  preview_note: "即時預覽",
  preview_empty_selector: "條件為空時符合全部成員",
  disabled: "停用",
  routes: "使用此群組的 route:",
  no_routes: "無",
  mcp: "MCP 端點:",
  save: "儲存",
  delete: "刪除群組",
  saved: "已儲存群組",
  deleted: "已刪除群組",
  delete_title: "刪除群組",
  delete_body: "只刪除群組本身;成員與其 tags 不受影響。被 route 引用的群組無法刪除。",
  no_members: "沒有符合的成員",
  tags_empty: "目前沒有任何 tag",
  name_required: "請填寫群組名稱",
});

const state = { groups: [], routes: [], editor: null };

// ---- 清單 ------------------------------------------------------------------------------------

function renderGroups() {
  const list = clear(byId("group-list"));
  if (!state.groups.length) {
    list.append(h("p", { class: "empty" }, S.empty_groups));
    return;
  }
  for (const g of state.groups) {
    const on = state.editor && state.editor.name === g.name;
    const btn = h("button", { type: "button", "data-group": g.name, "aria-current": on ? "true" : null },
      h("span", { class: "mono" }, g.name),
      h("span", { class: "muted" }, S.members_count(g.members.length)));
    btn.addEventListener("click", () => openEditor(g));
    list.append(btn);
  }
}

function renderTags(deployments) {
  const list = clear(byId("tag-list"));
  const byTag = new Map();
  for (const d of deployments) {
    for (const t of d.tags || []) {
      if (!byTag.has(t)) byTag.set(t, []);
      byTag.get(t).push(d);
    }
  }
  const tags = [...byTag.keys()].sort();
  if (!tags.length) {
    list.append(h("li", { class: "muted" }, S.tags_empty));
    return;
  }
  for (const t of tags) {
    const members = byTag.get(t);
    list.append(h("li", { "data-tag": t },
      h("span", { class: "chip" }, t),
      h("span", { class: "muted" }, S.members_count(members.length)),
      h("span", { class: "tag-members" }, ...members.map((d) =>
        h("a", { href: `/ui/members.html#${encodeURIComponent(d.id)}`, class: "mono", "data-member": d.id }, d.id)))));
  }
}

async function loadAll() {
  const list = byId("group-list");
  if (!state.groups.length) setLoading(list, true);
  try {
    const [groups, routes, deps] = await Promise.all([
      getJSON("/api/groups"), getJSON("/api/routes"), getJSON("/api/deployments")]);
    state.groups = groups.groups || [];
    state.routes = routes.routes || [];
    renderGroups();
    renderTags(deps.deployments || []);
  } catch (err) {
    setLoading(list, false);
    showError(err);
  }
}

// ---- 編輯器 ----------------------------------------------------------------------------------

function openEditor(group) {
  if (state.editor) state.editor.destroy();
  state.editor = createEditor(group);
  renderGroups();
}

function createEditor(group) {
  const isNew = !group;
  const name = group ? group.name : "";
  const sel = { all: [], any: [], none: [] };
  if (group) for (const [k] of ROWS) sel[k] = [...(group.selector[k] || [])];
  let timer = null;
  let reqSeq = 0;
  let destroyed = false;

  const section = clear(byId("group-editor"));
  const nameInput = h("input", { type: "text", id: "group-name", class: "mono", autocomplete: "off",
    value: name, disabled: !isNew, "aria-label": S.name });
  const rows = {};
  const rowEls = ROWS.map(([key, text]) => {
    const chips = h("span", { class: "row", "data-chips": key });
    const input = h("input", { type: "text", "data-tag-input": key, autocomplete: "off",
      placeholder: S.tag_placeholder, "aria-label": `${text} ${key} tag` });
    const add = h("button", { type: "button", class: "btn sm" }, S.tag_add);
    const commit = () => {
      const t = input.value.trim();
      if (!t) return;
      if (!sel[key].includes(t)) sel[key].push(t);
      input.value = "";
      renderChips(key);
      schedulePreview();
    };
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        commit();
      }
    });
    add.addEventListener("click", commit);
    rows[key] = { chips };
    return h("div", { class: "selrow", "data-row": key },
      h("div", {}, h("strong", {}, text), h("div", { class: "muted small" }, key)),
      h("div", { class: "row" }, chips, input, add));
  });
  const previewTitle = h("strong", { "data-role": "preview-title" });
  const previewList = h("ul", { class: "plain", "data-role": "preview" });
  const routesBox = h("div", { "data-role": "routes" });
  const mcpBox = h("div", { "data-role": "mcp" });
  const saveBtn = h("button", { type: "button", class: "btn primary", "data-role": "save" }, S.save);
  const delBtn = isNew ? null : h("button", { type: "button", class: "btn", "data-role": "delete" }, S.delete);

  section.append(
    h("div", { class: "row" }, h("h2", { id: "group-editor-title" }, isNew ? S.new_group : S.edit_group),
      h("label", { class: "row" }, S.name, nameInput)),
    h("div", {}, ...rowEls),
    h("div", { class: "stack tight" },
      h("div", { class: "preview-head" }, previewTitle, h("span", { class: "muted small" }, S.preview_note)),
      previewList),
    isNew ? null : h("div", { class: "row" }, routesBox, mcpBox),
    h("div", { class: "row end" }, delBtn, saveBtn));
  section.hidden = false;

  function renderChips(key) {
    const box = clear(rows[key].chips);
    if (!sel[key].length) box.append(h("span", { class: "muted" }, S.unset));
    for (const t of sel[key]) {
      const x = h("button", { type: "button", class: "chip-x", "aria-label": `${S.tag_remove} ${key} ${t}` }, "×");
      x.addEventListener("click", () => {
        sel[key] = sel[key].filter((v) => v !== t);
        renderChips(key);
        schedulePreview();
      });
      box.append(h("span", { class: key === "none" ? "chip red" : "chip blue", "data-tag": t }, t, x));
    }
  }

  function renderPreview(members) {
    setText(previewTitle, S.preview(members.length));
    clear(previewList);
    if (!members.length) previewList.append(h("li", { class: "muted" }, S.no_members));
    for (const m of members) {
      previewList.append(h("li", { "data-member": m.id },
        h("a", { href: `/ui/members.html#${encodeURIComponent(m.id)}`, class: "mono" }, m.id),
        kindBadge(m.kind),
        m.enabled ? null : h("span", { class: "badge b-muted" }, S.disabled)));
    }
  }

  async function runPreview() {
    timer = null;
    const mine = ++reqSeq;
    try {
      const r = await sendJSON("POST", "/api/groups/preview", { selector: { ...sel } });
      if (destroyed || mine !== reqSeq) return;
      renderPreview(r.members || []);
    } catch (err) {
      if (!destroyed && mine === reqSeq) showError(err);
    }
  }

  function schedulePreview() {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(runPreview, PREVIEW_DELAY_MS);
  }

  function renderRefs() {
    clear(routesBox);
    clear(mcpBox);
    const refs = state.routes.filter((r) => r.target && r.target.group === name).map((r) => r.name);
    routesBox.append(S.routes);
    if (!refs.length) routesBox.append(h("span", { class: "muted" }, S.no_routes));
    refs.forEach((r, i) => {
      if (i) routesBox.append(", ");
      routesBox.append(h("a", { href: `/ui/route.html?name=${encodeURIComponent(r)}`, class: "mono" }, r));
    });
    mcpBox.append(S.mcp, h("a", { href: "/ui/mcp.html", class: "mono" }, `/mcp/${encodeURIComponent(name)}`));
  }

  saveBtn.addEventListener("click", () => withBusy(saveBtn, async () => {
    const body = { selector: { all: [...sel.all], any: [...sel.any], none: [...sel.none] } };
    try {
      let saved;
      if (isNew) {
        const n = nameInput.value.trim();
        if (!n) {
          showError({ message: S.name_required });
          nameInput.focus();
          return;
        }
        saved = await sendJSON("POST", "/api/groups", { name: n, ...body });
      } else {
        saved = await sendJSON("PUT", `/api/groups/${encodeURIComponent(name)}`, body);
      }
      showMessage(`${S.saved} ${saved.name}`, "ok");
      await loadAll();
      openEditor(state.groups.find((g) => g.name === saved.name) || saved);
    } catch (err) {
      showError(err);
    }
  }));

  if (delBtn) {
    delBtn.addEventListener("click", async () => {
      const ok = await confirmDialog({ title: `${S.delete_title} ${name}`, body: S.delete_body,
        confirmLabel: S.delete, danger: true });
      if (!ok) return;
      await withBusy(delBtn, async () => {
        try {
          await sendJSON("DELETE", `/api/groups/${encodeURIComponent(name)}`);
          if (state.editor) state.editor.destroy();
          state.editor = null;
          showMessage(`${S.deleted} ${name}`, "ok");
          await loadAll();
        } catch (err) {
          showError(err);
        }
      });
    });
  }

  for (const [k] of ROWS) renderChips(k);
  if (!isNew) renderRefs();
  if (group && Array.isArray(group.members)) renderPreview(group.members);
  runPreview();
  (isNew ? nameInput : section.querySelector("[data-tag-input]")).focus();

  return {
    name,
    destroy() {
      destroyed = true;
      if (timer !== null) clearTimeout(timer);
      clear(section);
      section.hidden = true;
    },
  };
}

// ---- 啟動 ------------------------------------------------------------------------------------

byId("new-group").addEventListener("click", () => openEditor(null));
initShell();
loadAll();

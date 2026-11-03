// UI-3 群組與 Tag(CHG-20261103-01 T5;決策 13、24)。
// - 群組清單(名稱、符合的成員數);編輯器以 all / any / none 三列 tag 輸入(Enter 加入、× 移除),
//   每次變更後 300 ms 以 `POST /api/groups/preview` 即時預覽符合的成員。
// - 新增(POST)名稱違反規則時顯示 422 訊息;刪除被 route 引用的群組時顯示 409 訊息(含 route 名)。
// - 「所有 Tag」:列出全部 tag 與各 tag 的成員;點成員開啟 UI-2 編輯表單(members.html#<id>)。
//   tag 改名不做(決策 24)。

import { getJSON, sendJSON } from "./api.js";
import { byId, clear, h, setText } from "./dom.js";
import { TEXT } from "./strings.js";
import {
  confirmDialog, initShell, kindBadge, latest, memberHref, setLoading, showError, showMessage, tagInput, withBusy,
} from "./ui.js";

const PREVIEW_DELAY_MS = 300;
const ROWS = [
  ["all", "全部包含"],
  ["any", "任一包含"],
  ["none", "排除"],
];

const T = Object.freeze({
  members_count: (n) => `${n} 個成員`,
  empty_groups: "尚未建立群組",
  new_group: "新增群組",
  edit_group: "群組條件",
  name: "名稱",
  preview: (n) => `符合的成員(${n})`,
  preview_note: "即時預覽",
  preview_failed: "預覽失敗",
  routes: "使用此群組的 route:",
  no_routes: "無",
  mcp: "MCP 端點:",
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

function memberLink(id, attrs = {}) {
  return h("a", { href: memberHref(id), class: "mono", ...attrs }, id);
}

// ---- 清單 ------------------------------------------------------------------------------------

function renderGroups() {
  const list = clear(byId("group-list"));
  if (!state.groups.length) {
    list.append(h("p", { class: "empty" }, T.empty_groups));
    return;
  }
  for (const g of state.groups) {
    const on = state.editor && state.editor.name === g.name;
    const btn = h("button", { type: "button", "data-group": g.name, "aria-current": on ? "true" : null },
      h("span", { class: "mono" }, g.name),
      h("span", { class: "muted" }, T.members_count(g.members.length)));
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
    list.append(h("li", { class: "muted" }, T.tags_empty));
    return;
  }
  for (const t of tags) {
    const members = byTag.get(t);
    list.append(h("li", { "data-tag": t },
      h("span", { class: "chip" }, t),
      h("span", { class: "muted" }, T.members_count(members.length)),
      h("span", { class: "chips" }, ...members.map((d) =>
        memberLink(d.id, { "data-member": d.id })))));
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
  const previewGuard = latest();
  let destroyed = false;

  const section = clear(byId("group-editor"));
  const nameInput = h("input", { type: "text", id: "group-name", class: "mono", autocomplete: "off",
    value: name, disabled: !isNew, "aria-label": T.name });
  const boxes = [];
  const rowEls = ROWS.map(([key, text]) => {
    const box = tagInput({
      tags: sel[key],
      chipClass: key === "none" ? "chip red" : "chip blue",
      removeLabel: (t) => `${TEXT.tag_remove} ${key} ${t}`,
      chipsTag: "span",
      chipsAttrs: { "data-chips": key },
      inputAttrs: { "data-tag-input": key, "aria-label": `${text} ${key} tag` },
      onChange: (tags) => {
        sel[key] = tags;
        schedulePreview();
      },
    });
    boxes.push(box);
    return h("div", { class: "selrow", "data-row": key },
      h("div", {}, h("strong", {}, text), h("div", { class: "muted small" }, key)),
      h("div", { class: "row" }, box.chips, box.input, box.button));
  });
  const previewTitle = h("strong", { "data-role": "preview-title" });
  const previewList = h("ul", { class: "plain", "data-role": "preview" });
  const routesBox = h("div", { "data-role": "routes" });
  const mcpBox = h("div", { "data-role": "mcp" });
  const saveBtn = h("button", { type: "button", class: "btn primary", "data-role": "save" }, TEXT.save);
  const delBtn = isNew ? null : h("button", { type: "button", class: "btn", "data-role": "delete" }, T.delete);

  section.append(
    h("div", { class: "row" }, h("h2", { id: "group-editor-title" }, isNew ? T.new_group : T.edit_group),
      h("label", { class: "row" }, T.name, nameInput)),
    h("div", {}, ...rowEls),
    h("div", { class: "stack tight" },
      h("div", { class: "preview-head" }, previewTitle, h("span", { class: "muted small" }, T.preview_note)),
      previewList),
    isNew ? null : h("div", { class: "row" }, routesBox, mcpBox),
    h("div", { class: "row end" }, delBtn, saveBtn));
  section.hidden = false;

  function renderPreview(members) {
    previewList.removeAttribute("data-stale");
    previewList.removeAttribute("aria-busy");
    setText(previewTitle, T.preview(members.length));
    clear(previewList);
    if (!members.length) previewList.append(h("li", { class: "muted" }, T.no_members));
    for (const m of members) {
      previewList.append(h("li", { "data-member": m.id },
        memberLink(m.id),
        kindBadge(m.kind),
        m.enabled ? null : h("span", { class: "badge b-muted" }, TEXT.disabled)));
    }
  }

  async function runPreview() {
    timer = null;
    const isLatest = previewGuard();
    try {
      const r = await sendJSON("POST", "/api/groups/preview", { selector: { ...sel } });
      if (destroyed || !isLatest()) return;
      renderPreview(r.members || []);
    } catch (err) {
      if (destroyed || !isLatest()) return;
      // 預覽失敗:清掉舊結果並標為過時,畫面不留與目前條件不符的成員集合(審議 M4)
      clear(previewList);
      previewList.setAttribute("data-stale", "true");
      previewList.removeAttribute("aria-busy");
      setText(previewTitle, T.preview_failed);
      showError(err);
    }
  }

  function schedulePreview() {
    if (timer !== null) clearTimeout(timer);
    previewList.setAttribute("aria-busy", "true");
    timer = setTimeout(runPreview, PREVIEW_DELAY_MS);
  }

  function renderRefs() {
    clear(routesBox);
    clear(mcpBox);
    const refs = state.routes.filter((r) => r.target && r.target.group === name).map((r) => r.name);
    routesBox.append(T.routes);
    if (!refs.length) routesBox.append(h("span", { class: "muted" }, T.no_routes));
    refs.forEach((r, i) => {
      if (i) routesBox.append(", ");
      routesBox.append(h("a", { href: `/ui/route.html?name=${encodeURIComponent(r)}`, class: "mono" }, r));
    });
    mcpBox.append(T.mcp, h("a", { href: `/ui/mcp.html?group=${encodeURIComponent(name)}`, class: "mono" }, `/mcp/${encodeURIComponent(name)}`));
  }

  saveBtn.addEventListener("click", () => withBusy(saveBtn, async () => {
    // 已輸入但沒按 Enter 的 tag 也要納入(commit 會經 onChange 更新 sel)
    for (const box of boxes) box.commit();
    const body = { selector: { all: [...sel.all], any: [...sel.any], none: [...sel.none] } };
    try {
      let saved;
      if (isNew) {
        const n = nameInput.value.trim();
        if (!n) {
          showError({ message: T.name_required });
          nameInput.focus();
          return;
        }
        saved = await sendJSON("POST", "/api/groups", { name: n, ...body });
      } else {
        saved = await sendJSON("PUT", `/api/groups/${encodeURIComponent(name)}`, body);
      }
      showMessage(`${T.saved} ${saved.name}`, "ok");
      await loadAll();
      // 儲存期間使用者已切到別的群組:不把畫面切回來
      if (!destroyed) openEditor(state.groups.find((g) => g.name === saved.name) || saved);
    } catch (err) {
      showError(err);
    }
  }));

  if (delBtn) {
    delBtn.addEventListener("click", async () => {
      const ok = await confirmDialog({ title: `${T.delete_title} ${name}`, body: T.delete_body,
        confirmLabel: T.delete, danger: true });
      if (!ok) return;
      await withBusy(delBtn, async () => {
        try {
          await sendJSON("DELETE", `/api/groups/${encodeURIComponent(name)}`);
          if (state.editor) state.editor.destroy();
          state.editor = null;
          showMessage(`${T.deleted} ${name}`, "ok");
          await loadAll();
        } catch (err) {
          showError(err);
        }
      });
    });
  }

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

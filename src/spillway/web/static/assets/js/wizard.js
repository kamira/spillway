// 首次設定精靈(CHG-20261103-01 T8;決策 18、25,決策審議修訂 AM-8;設計差異 #10)。
// 四步:① 加入成員 → ② 測試呼叫 → ③ 建立 route → ④ 完成;每步完成即寫入(中途離開不回滾)。
// ① 共用 member-form.js(預設類型 api、tags 預填 default、至少 1 個 tag)。
// ② POST /api/deployments/{id}/test(會實際呼叫上游一次並記一列帳本);失敗時顯示錯誤並可回上一步。
//    「下一步」不以測試成功為前提(測試是建議步驟;CLI 成員可不經測試直接建立 route)。
// ③ 名稱預設 chat;mode 預設 chat(① 選了 cli 時預設 task 並提示);target 為 {tags: {any: [<所選 tag>]}},
//    tag 選單依 ① 的輸入順序(由畫面保存,不取 API 回傳的排序),以 POST /api/groups/preview 顯示符合數;
//    route 已存在(409)時沿用既有 route,直接進入 ④。
// ④ 可複製的 curl 範例、「前往總覽」、「前往設定產生」(settings.html#token)。
// 續作:直接開啟時依現況決定起點——無成員 → ①;有成員無 route → ③;都有 → ④。頁面載入只發 GET。

import { getJSON, sendJSON } from "./api.js";
import { byId, clear, h, setText } from "./dom.js";
import { STATUS, TEXT, label } from "./strings.js";
import { errorText, initShell, latest, showError, showMessage, withBusy } from "./ui.js";
import { mountMemberForm } from "./member-form.js";

const T = Object.freeze({
  next: "下一步 →",
  test_ok: (ms) => `測試成功 · 延遲 ${ms ?? TEXT.dash} ms`,
  test_failed: (status) => `測試失敗:${status}`,
  test_hint: "請檢查憑證參照的環境變數(必須在啟動 serve 前設定)與上游網址,可回上一步修改成員。",
  test_reply: "回應:",
  preview: (n) => `符合 ${n} 個成員`,
  no_tags: "成員沒有任何 tag;請回「成員」頁加入 tag 後再建立 route",
  err_name: "請填寫 route 名稱(不可含空白、/ 與 :)",
  err_tag: "請選擇 tag",
  reused: (name) => `route「${name}」已存在,沿用既有 route`,
  created: (name) => `已建立 route「${name}」`,
  done_lead: (name) => `route「${name}」已就緒。以下列指令送出第一次呼叫:`,
  copied: "已複製到剪貼簿",
  copy_failed: "無法寫入剪貼簿(權限被拒或不支援)。已選取文字,請按 ⌘C(macOS)或 Ctrl+C 複製。",
});

const state = {
  step: 0,
  port: null,
  member: null,      // ① 建立(或編輯)後的成員
  kind: null,        // 決定 ③ 的 mode 預設
  tags: [],          // ③ 的 tag 選單(① 的輸入順序)
  route: null,       // ④ 的 route 名稱
  form: null,        // mountMemberForm 的 handle
};
const previewGuard = latest();

// ---- 步驟切換 -------------------------------------------------------------------------------

function show(step) {
  state.step = step;
  byId("wizard-loading").hidden = true;
  for (const n of [1, 2, 3, 4]) byId(`step-${n}`).hidden = n !== step;
  for (const li of byId("wizard-steps").querySelectorAll("li[data-step]")) {
    const n = Number(li.getAttribute("data-step"));
    li.classList.toggle("done", n < step);
    li.classList.toggle("cur", n === step);
    if (n === step) li.setAttribute("aria-current", "step");
    else li.removeAttribute("aria-current");
  }
  byId("main").setAttribute("data-step", String(step));
}

// ---- ① 加入成員 -----------------------------------------------------------------------------

// 送出時 tag chip 的順序就是使用者的輸入順序(API 回傳的 tags 依字母排序)
function chipTags(form) {
  return [...form.querySelectorAll('[data-role="tags"] [data-tag]')].map((el) => el.getAttribute("data-tag"));
}

async function onMemberSaved(member) {
  const order = state.form ? chipTags(state.form.element) : [];
  const saved = Array.isArray(member.tags) ? member.tags : [];
  state.member = member;
  state.kind = member.kind;
  state.tags = order.filter((t) => saved.includes(t)).concat(saved.filter((t) => !order.includes(t)));
  if (state.form) {
    state.form.destroy();
    state.form = null;
  }
  enterTest();
}

async function enterMember() {
  const host = byId("member-form-host");
  if (state.form) state.form.destroy();
  clear(host);
  let initial = null;
  if (state.member) {
    // 從 ② 回上一步:成員已寫入,改為編輯(PUT 全量)
    try {
      initial = await getJSON(`/api/deployments/${encodeURIComponent(state.member.id)}`);
    } catch (err) {
      showError(err);
      initial = state.member;
    }
  }
  state.form = mountMemberForm(host, {
    initial,
    defaults: { kind: "api", tags: ["default"] },
    requireTag: true,
    submitLabel: T.next,
    onSaved: onMemberSaved,
  });
  show(1);
}

// ---- ② 測試呼叫 -----------------------------------------------------------------------------

function enterTest() {
  const m = state.member;
  setText(byId("test-member"), `${m.id} · ${m.provider} / ${m.model}`);
  const box = byId("test-result");
  box.hidden = true;
  box.className = "callout";
  clear(box);
  show(2);
}

function testResult(ok, lines) {
  const box = byId("test-result");
  clear(box);
  box.className = ok ? "callout ok" : "callout err";
  box.setAttribute("data-ok", ok ? "true" : "false");
  box.append(...lines.filter(Boolean));
  box.hidden = false;
}

async function runTest() {
  const id = state.member.id;
  await withBusy(byId("test-run"), async () => {
    try {
      const r = await sendJSON("POST", `/api/deployments/${encodeURIComponent(id)}/test`);
      if (r.ok) {
        testResult(true, [
          h("strong", { "data-role": "test-summary" }, T.test_ok(r.latency_ms)),
          r.text ? h("div", { class: "mono small", "data-role": "test-text" }, `${T.test_reply}${r.text}`) : null,
        ]);
      } else {
        testResult(false, [
          h("strong", { "data-role": "test-summary", title: r.status || "" }, T.test_failed(label(STATUS, r.status))),
          r.error ? h("div", { class: "mono small", "data-role": "test-error" }, r.error) : null,
          h("div", { "data-role": "test-hint" }, T.test_hint),
        ]);
      }
    } catch (err) {
      testResult(false, [
        h("strong", { "data-role": "test-summary" }, T.test_failed(errorText(err))),
        h("div", { "data-role": "test-hint" }, T.test_hint),
      ]);
    }
  });
}

// ---- ③ 建立 route ---------------------------------------------------------------------------

async function updatePreview() {
  const out = byId("route-preview");
  const tag = byId("route-tag").value;
  const isLatest = previewGuard();
  if (!tag) {
    setText(out, TEXT.dash);
    return;
  }
  try {
    const r = await sendJSON("POST", "/api/groups/preview", { selector: { all: [], any: [tag], none: [] } });
    if (!isLatest()) return;
    const n = (r.members || []).length;
    setText(out, T.preview(n));
    out.setAttribute("data-count", String(n));
  } catch (err) {
    if (!isLatest()) return;
    setText(out, errorText(err));
    out.removeAttribute("data-count");
  }
}

function routeError(text) {
  const box = byId("route-error");
  setText(box, text || "");
  box.hidden = !text;
}

function enterRoute() {
  const sel = byId("route-tag");
  clear(sel);
  for (const t of state.tags) sel.append(h("option", { value: t }, t));
  if (state.tags.length) sel.value = state.tags[0];
  const cli = state.kind === "cli";
  byId("route-mode").value = cli ? "task" : "chat";
  byId("route-cli-note").hidden = !cli;
  byId("route-create").disabled = !state.tags.length;
  routeError(state.tags.length ? null : T.no_tags);
  show(3);
  updatePreview();
}

async function createRoute(ev) {
  ev.preventDefault();
  routeError(null);
  const name = byId("route-name").value.trim();
  if (!name || /[\s/:]/.test(name)) {
    routeError(T.err_name);
    byId("route-name").focus();
    return;
  }
  const tag = byId("route-tag").value;
  if (!tag) {
    routeError(T.err_tag);
    return;
  }
  const body = { name, mode: byId("route-mode").value, target: { tags: { any: [tag] } } };
  await withBusy(byId("route-create"), async () => {
    try {
      await sendJSON("POST", "/api/routes", body);
      showMessage(T.created(name), "ok");
    } catch (err) {
      if (err && err.status === 409) {
        showMessage(T.reused(name), "info");
      } else {
        routeError(errorText(err));
        return;
      }
    }
    state.route = name;
    enterDone();
  });
}

// ---- ④ 完成 ---------------------------------------------------------------------------------

function shellQuote(text) {
  return `'${String(text).replace(/'/g, "'\\''")}'`;
}

export function curlExample(port, route) {
  const body = JSON.stringify({ model: route, messages: [{ role: "user", content: "hello" }] });
  return `curl http://127.0.0.1:${port}/v1/chat/completions \\\n  -H "Content-Type: application/json" \\\n  -d ${shellQuote(body)}`;
}

function enterDone() {
  setText(byId("done-lead"), T.done_lead(state.route));
  const pre = byId("curl-example");
  setText(pre, curlExample(state.port, state.route));
  pre.setAttribute("data-route", state.route);
  show(4);
}

async function copyCurl() {
  const pre = byId("curl-example");
  const hint = byId("curl-copy-hint");
  hint.hidden = true;
  try {
    if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error("no clipboard");
    await navigator.clipboard.writeText(pre.textContent);
    showMessage(T.copied, "ok");
  } catch {
    const sel = window.getSelection();
    if (sel) {
      sel.removeAllRanges();
      const range = document.createRange();
      range.selectNodeContents(pre);
      sel.addRange(range);
    }
    setText(hint, T.copy_failed);
    hint.hidden = false;
    pre.focus();
  }
}

// ---- 續作與啟動 -----------------------------------------------------------------------------

// 有成員無 route 時從 ③ 開始:tag 選單列出既有成員的 tags(依成員與 tag 的 API 順序)
async function resumeRoute() {
  const data = await getJSON("/api/deployments");
  const deps = data.deployments || [];
  const tags = [];
  for (const d of deps) for (const t of d.tags || []) if (!tags.includes(t)) tags.push(t);
  state.tags = tags;
  state.kind = deps.length && deps.every((d) => d.kind === "cli") ? "cli" : null;
  enterRoute();
}

async function resumeDone() {
  const data = await getJSON("/api/routes");
  const names = (data.routes || []).map((r) => r.name);
  state.route = names.includes("chat") ? "chat" : names[0];
  enterDone();
}

async function start() {
  try {
    const s = await getJSON("/api/settings");
    state.port = s.port ?? window.location.port;
    const counts = s.counts || {};
    if (!counts.deployments) await enterMember();
    else if (!counts.routes) await resumeRoute();
    else await resumeDone();
  } catch (err) {
    setText(byId("wizard-loading"), errorText(err));
    showError(err);
  }
}

function init() {
  byId("test-run").addEventListener("click", runTest);
  byId("test-back").addEventListener("click", () => enterMember());
  byId("test-next").addEventListener("click", enterRoute);
  byId("route-tag").addEventListener("change", updatePreview);
  byId("route-form").addEventListener("submit", createRoute);
  byId("curl-copy").addEventListener("click", copyCurl);
  initShell();
  start();
}

init();

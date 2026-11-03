// 成員表單(CHG-20261103-01 T5;決策 12,決策審議修訂 AM-7)。UI-2 與首次設定精靈(T8)共用。
//
// mountMemberForm(container, {initial, onSaved, onCancel, defaults, requireTag, submitLabel}) -> {destroy(), element}
// - initial:既有成員(`GET /api/deployments/{id}` 的回應)→ 編輯(PUT 全量);null → 新增(POST)。
// - defaults:新增時的預設值,例如 {kind: "api", tags: ["default"]}(精靈);可含 id、model_name、
//   credential_ref、params、enabled。
// - requireTag:true 時至少 1 個 tag 才送出(精靈)。
// - onSaved(member, {created}):送出成功後以伺服器回應呼叫。onCancel():按「取消」(未提供時不顯示按鈕)。
// - 連動選單(spec 10.2):類型 → 供應商 → 模型,選單內容取自 `GET /api/adapters`;模型以 label 顯示、
//   以 id 寫入,最後一項「其他(手動輸入)」;不可用模組與已有成員的 single 模組停用並顯示原因。
// - 上游網址與 params.api_base 雙向同步;送出時以上游網址欄為準。
// 錯誤一律顯示 error.message(頂端訊息列);不輸出任何內容到 console。

import { getJSON, sendJSON } from "./api.js";
import { clear, h, setText } from "./dom.js";
import { BUDGET_TYPE, KIND, TEXT, WINDOW, label } from "./strings.js";
import { showError, showMessage, withBusy } from "./ui.js";

const OTHER = "__other__";
const EFFORTS = ["none", "low", "medium", "high", "max"];
const KINDS = ["cli", "api", "local"];

// 本表單專用的字串(共用字串在 strings.js)
const S = Object.freeze({
  kind: "類型",
  id: "成員 id",
  provider: "供應商",
  model: "模型",
  other: "其他(手動輸入)",
  model_manual: "模型 id(手動輸入)",
  llm_provider: "LiteLLM provider",
  llm_provider_hint: "例:deepseek、gemini、ollama、hosted_vllm;填 litellm 時模型欄須為完整的 LiteLLM 模型字串",
  model_name: "model_name",
  model_name_hint: "呼叫端在 model 欄位使用的名稱;預設等於所選模型的 id",
  upstream: "上游網址",
  upstream_hint_api: "寫入 params.api_base;API 可留空(使用供應商預設端點)",
  upstream_hint_local: "寫入 params.api_base;Local 必填",
  credential: "憑證參照",
  credential_hint: "填環境變數名或 keyring 項目(env:NAME、keyring:服務:帳號),不要填金鑰本身",
  caps: "能力",
  ctx: "Context 上限(tokens)",
  features: "功能",
  efforts: "支援的 effort",
  budget: "額度",
  budget_type: "額度類型",
  limit: "上限(USD)",
  limit_hint: "空白表示無上限",
  window: "週期",
  anchor: "重置日(1–28)",
  tags: "Tag",
  tag_placeholder: "輸入 tag 後按 Enter",
  tag_add: "+ tag",
  tag_remove: "移除",
  tag_none: "未設定",
  enabled: "啟用",
  params: "params(JSON)",
  params_help_title: "CLI 常用鍵",
  params_help: [
    ["effort_args", "effort 參數樣板,例 [\"--effort\", \"{effort}\"]"],
    ["extra_args", "附加的 CLI 參數清單,例 [\"--strict-mcp-config\"]"],
    ["max_concurrency", "同時執行上限(整數)"],
    ["timeout", "逾時秒數"],
  ],
  save: "儲存",
  cancel: "取消",
  loading_adapters: "載入中…",
  choose_provider: "— 無可用的供應商 —",
  single_full: "已有 1 個,僅限 1 個",
  unavailable: "模組不可用",
  models_error: "無法取得執行期模型清單,只列 models.yaml:",
  err_id: "請填寫成員 id(不可含空白或 /)",
  err_provider: "請選擇可用的供應商",
  err_model: "請選擇或輸入模型",
  err_model_name: "model_name 為必填",
  err_upstream: "Local 成員必須填寫上游網址",
  err_params: "params 必須是 JSON 物件",
  err_ctx: "Context 上限必須是正整數",
  err_limit: "上限必須是 0 以上的數字",
  err_anchor: "重置日必須是 1–28 的整數",
  err_tag: "至少需要 1 個 tag",
  sync_note: "params.api_base 與上游網址不同,已以上游網址為準",
  saved: "已儲存成員",
});

let seq = 0;

function deepCopy(v) {
  return v === undefined ? undefined : JSON.parse(JSON.stringify(v));
}

function field(labelText, control, { hint, id } = {}) {
  return h("label", { class: "f", for: id || null }, labelText, control,
    hint ? h("span", { class: "hint" }, hint) : null);
}

export function mountMemberForm(container, {
  initial = null, onSaved = null, onCancel = null, defaults = {}, requireTag = false, submitLabel = S.save,
} = {}) {
  const uid = `mf${++seq}`;
  const editing = initial !== null && initial !== undefined;
  const base = editing ? deepCopy(initial) : {
    id: defaults.id || "",
    model_name: defaults.model_name || "",
    kind: KINDS.includes(defaults.kind) ? defaults.kind : "api",
    provider: defaults.provider || "",
    model: defaults.model || "",
    params: deepCopy(defaults.params) || {},
    credential_ref: defaults.credential_ref || null,
    capabilities: deepCopy(defaults.capabilities) || {},
    enabled: defaults.enabled !== false,
    tags: Array.isArray(defaults.tags) ? [...defaults.tags] : [],
    budget: deepCopy(defaults.budget) || null,
  };

  const state = {
    kind: base.kind,
    modules: null,            // /api/adapters 的 modules;null = 載入中
    tags: [...(base.tags || [])],
    modelNameAuto: !editing && !base.model_name,
    destroyed: false,
  };

  // ---- 控制項 ----
  const fid = (name) => `${uid}-${name}`;
  const idInput = h("input", { type: "text", id: fid("id"), name: "id", class: "mono", autocomplete: "off",
    value: base.id || "", disabled: editing });
  const kindButtons = KINDS.map((k) => h("button", { type: "button", "data-kind": k,
    "aria-pressed": k === state.kind ? "true" : "false", class: k === state.kind ? "on" : null }, KIND[k]));
  const kindSeg = h("div", { class: "seg", role: "group", "aria-labelledby": fid("kind-l") }, ...kindButtons);
  const providerSel = h("select", { id: fid("provider"), name: "provider", disabled: true },
    h("option", { value: "" }, S.loading_adapters));
  const providerNote = h("span", { class: "hint", "data-role": "provider-note" });
  const llmProviderInput = h("input", { type: "text", id: fid("llm-provider"), name: "llm_provider",
    class: "mono", autocomplete: "off" });
  const llmProviderField = field(S.llm_provider, llmProviderInput, { hint: S.llm_provider_hint, id: fid("llm-provider") });
  const modelSel = h("select", { id: fid("model"), name: "model", disabled: true },
    h("option", { value: "" }, S.loading_adapters));
  const modelsError = h("span", { class: "hint warn-text", "data-role": "models-error", hidden: true });
  const modelManual = h("input", { type: "text", id: fid("model-manual"), name: "model_manual", class: "mono",
    autocomplete: "off" });
  const modelManualField = field(S.model_manual, modelManual, { id: fid("model-manual") });
  const modelNameInput = h("input", { type: "text", id: fid("model-name"), name: "model_name", class: "mono",
    autocomplete: "off", value: base.model_name || "", required: true });
  const upstreamInput = h("input", { type: "url", id: fid("upstream"), name: "upstream", class: "mono",
    autocomplete: "off", placeholder: "http://127.0.0.1:11434/v1" });
  const upstreamHint = h("span", { class: "hint" });
  const upstreamField = h("label", { class: "f", for: fid("upstream") }, S.upstream, upstreamInput, upstreamHint);
  const credInput = h("input", { type: "text", id: fid("credential"), name: "credential_ref", class: "mono",
    autocomplete: "off", spellcheck: "false", placeholder: "env:OPENAI_API_KEY",
    value: base.credential_ref || "" });
  const ctxInput = h("input", { type: "number", id: fid("ctx"), name: "ctx", min: "1", step: "1" });
  const toolsBox = h("input", { type: "checkbox", name: "tools" });
  const visionBox = h("input", { type: "checkbox", name: "vision" });
  const effortBoxes = EFFORTS.map((e) => h("input", { type: "checkbox", name: "effort", value: e }));
  const budgetType = h("select", { id: fid("budget-type"), name: "budget_type" },
    ...["none", "usd", "credit_pool"].map((t) => h("option", { value: t, title: t }, label(BUDGET_TYPE, t))));
  const limitInput = h("input", { type: "number", id: fid("limit"), name: "limit_amount", min: "0", step: "any" });
  const windowSel = h("select", { id: fid("window"), name: "window" },
    ...["monthly", "weekly", "daily", "none"].map((w) => h("option", { value: w, title: w }, label(WINDOW, w))));
  const anchorInput = h("input", { type: "number", id: fid("anchor"), name: "reset_anchor", min: "1", max: "28", step: "1" });
  const anchorField = field(S.anchor, anchorInput, { id: fid("anchor") });
  const budgetFields = h("div", { class: "grid2", "data-role": "budget-fields" },
    field(S.limit, limitInput, { hint: S.limit_hint, id: fid("limit") }),
    field(S.window, windowSel, { id: fid("window") }),
    anchorField);
  const tagList = h("div", { class: "row", "data-role": "tags" });
  const tagInput = h("input", { type: "text", id: fid("tag"), name: "tag", autocomplete: "off",
    placeholder: S.tag_placeholder, "aria-label": "新增 tag" });
  const tagAdd = h("button", { type: "button", class: "btn sm" }, S.tag_add);
  const enabledBox = h("input", { type: "checkbox", name: "enabled", checked: base.enabled !== false });
  const paramsInput = h("textarea", { id: fid("params"), name: "params", class: "mono", autocomplete: "off",
    spellcheck: "false", rows: "6" });
  const formError = h("p", { class: "f-error", role: "alert", "data-role": "form-error", hidden: true });
  const submitBtn = h("button", { type: "submit", class: "btn primary", "data-role": "submit", disabled: true }, submitLabel);
  const cancelBtn = onCancel ? h("button", { type: "button", class: "btn", "data-role": "cancel" }, S.cancel) : null;

  const form = h("form", { class: "member-form stack", novalidate: true, "data-member-form": editing ? "edit" : "create" },
    h("div", { class: "f" }, h("span", { id: fid("kind-l") }, S.kind), kindSeg),
    h("div", { class: "grid2" },
      field(S.id, idInput, { id: fid("id") }),
      h("div", { class: "f" }, h("label", { for: fid("provider") }, S.provider), providerSel, providerNote),
      h("div", { class: "f" }, h("label", { for: fid("model") }, S.model), modelSel, modelsError),
      field(S.model_name, modelNameInput, { hint: S.model_name_hint, id: fid("model-name") }),
      modelManualField,
      llmProviderField),
    upstreamField,
    field(S.credential, credInput, { hint: S.credential_hint, id: fid("credential") }),
    h("hr"),
    h("h3", {}, S.caps),
    h("div", { class: "grid2" },
      field(S.ctx, ctxInput, { id: fid("ctx") }),
      h("div", { class: "f" }, h("span", {}, S.features), h("div", { class: "row" },
        h("label", { class: "check" }, toolsBox, "tools"),
        h("label", { class: "check" }, visionBox, "vision")))),
    h("div", { class: "f" }, h("span", {}, S.efforts), h("div", { class: "row", "data-role": "efforts" },
      ...effortBoxes.map((b) => h("label", { class: "check" }, b, b.value)))),
    h("hr"),
    h("h3", {}, S.budget),
    field(S.budget_type, budgetType, { id: fid("budget-type") }),
    budgetFields,
    h("hr"),
    h("h3", {}, S.tags),
    tagList,
    h("div", { class: "row" }, tagInput, tagAdd),
    h("label", { class: "check" }, enabledBox, S.enabled),
    h("hr"),
    h("div", { class: "f" },
      h("label", { for: fid("params") }, S.params),
      h("div", { class: "hint params-help" }, h("strong", {}, S.params_help_title),
        h("ul", { class: "params-keys" }, ...S.params_help.map(([k, d]) => h("li", {}, h("code", {}, k), `:${d}`)))),
      paramsInput),
    formError,
    h("div", { class: "row end" }, cancelBtn, submitBtn));

  // ---- 初值 ----
  setParamsText(base.params || {});
  upstreamInput.value = typeof (base.params || {}).api_base === "string" ? base.params.api_base : "";
  setCaps(base.capabilities || {});
  if (base.budget) {
    budgetType.value = base.budget.type;
    limitInput.value = base.budget.limit_amount === null || base.budget.limit_amount === undefined ? "" : String(base.budget.limit_amount);
    windowSel.value = base.budget.window || "monthly";
    anchorInput.value = base.budget.reset_anchor ? String(base.budget.reset_anchor) : "";
  } else {
    budgetType.value = "none";
  }
  renderTags();
  syncBudget();
  syncKindFields();
  modelManualField.hidden = true;
  llmProviderField.hidden = true;

  // ---- 輔助 ----
  function setParamsText(obj) {
    paramsInput.value = JSON.stringify(obj, null, 2);
  }

  function setCaps(caps) {
    ctxInput.value = Number.isInteger(caps.ctx) ? String(caps.ctx) : "";
    toolsBox.checked = caps.tools === true;
    visionBox.checked = caps.vision === true;
    const effs = Array.isArray(caps.efforts) ? caps.efforts : [];
    for (const b of effortBoxes) b.checked = effs.includes(b.value);
  }

  function syncBudget() {
    const none = budgetType.value === "none";
    budgetFields.hidden = none;
    anchorField.hidden = none || windowSel.value !== "monthly";
  }

  function syncKindFields() {
    const showUp = state.kind === "api" || state.kind === "local";
    upstreamField.hidden = !showUp;
    setText(upstreamHint, state.kind === "local" ? S.upstream_hint_local : S.upstream_hint_api);
    upstreamInput.required = state.kind === "local";
  }

  function renderTags() {
    clear(tagList);
    if (!state.tags.length) tagList.append(h("span", { class: "muted" }, S.tag_none));
    for (const t of state.tags) {
      const x = h("button", { type: "button", class: "chip-x", "aria-label": `${S.tag_remove} ${t}` }, "×");
      x.addEventListener("click", () => {
        state.tags = state.tags.filter((v) => v !== t);
        renderTags();
      });
      tagList.append(h("span", { class: "chip", "data-tag": t }, t, x));
    }
  }

  function addTag() {
    const t = tagInput.value.trim();
    if (!t) return;
    if (!state.tags.includes(t)) state.tags.push(t);
    tagInput.value = "";
    renderTags();
  }

  function moduleOf(provider) {
    return (state.modules || []).find((m) => m.providers.includes(provider)) || null;
  }

  function fallbackModule(kind) {
    return (state.modules || []).find((m) => m.available && (m.fallback_for_kinds || []).includes(kind)) || null;
  }

  function selectedModule() {
    const opt = providerSel.selectedOptions[0];
    return opt ? moduleOf(opt.value) : null;
  }

  function isFallback(mod) {
    return !!mod && (mod.fallback_for_kinds || []).length > 0;
  }

  // 供應商選單:支援所選類型的可用模組 + 全部不可用模組(停用並顯示原因)
  function renderProviders(preferred) {
    clear(providerSel);
    const kind = state.kind;
    const opts = [];
    for (const m of state.modules) {
      if (m.available && !m.kinds.includes(kind)) continue;
      for (const p of m.providers) {
        let text = p;
        let disabled = false;
        let reason = "";
        const own = editing && initial.provider === p;
        if (!m.available) {
          reason = `${S.unavailable}:${m.unavailable_reason || ""}`;
          disabled = !own;
        } else if (m.cardinality === "single" && m.existing_members > 0 && !own) {
          reason = S.single_full;
          disabled = true;
        }
        if (reason) text = `${p}(${reason})`;
        opts.push(h("option", { value: p, disabled, title: reason || m.module, "data-module": m.module,
          "data-reason": reason || null }, text));
      }
    }
    // 編輯既有成員:供應商不在選單時仍顯示原值
    let pick = preferred;
    if (preferred && !opts.some((o) => o.value === preferred)) {
      const fb = moduleOf(preferred) ? null : fallbackModule(kind);
      if (fb) {
        pick = fb.providers[0];
      } else {
        opts.push(h("option", { value: preferred, "data-module": "", "data-reason": S.unavailable,
          title: S.unavailable }, `${preferred}(${S.unavailable})`));
      }
    }
    providerSel.append(...opts);
    if (!opts.length) providerSel.append(h("option", { value: "" }, S.choose_provider));
    const usable = opts.filter((o) => !o.disabled);
    if (pick && opts.some((o) => o.value === pick && !o.disabled)) providerSel.value = pick;
    else if (usable.length) providerSel.value = usable[0].value;
    else providerSel.value = opts.length ? opts[0].value : "";
    providerSel.disabled = false;
    updateProviderNote();
  }

  function updateProviderNote() {
    const opt = providerSel.selectedOptions[0];
    setText(providerNote, opt && opt.getAttribute("data-reason") ? opt.getAttribute("data-reason") : "");
  }

  function modelsOf(mod) {
    return mod && mod.available ? mod.models || [] : [];
  }

  function renderModels(preferredModel, preferredProvider) {
    const mod = selectedModule();
    clear(modelSel);
    const models = modelsOf(mod);
    for (const m of models) {
      const key = m.provider ? `${m.provider}\u0000${m.id}` : m.id;
      modelSel.append(h("option", { value: key, "data-id": m.id, title: m.id }, m.label || m.id));
    }
    modelSel.append(h("option", { value: OTHER }, S.other));
    modelSel.disabled = false;
    const err = mod && mod.models_error;
    modelsError.hidden = !err;
    setText(modelsError, err ? `${S.models_error}${err}` : "");
    llmProviderField.hidden = !isFallback(mod);
    let match = null;
    if (preferredModel) {
      match = models.find((m) => m.id === preferredModel && (!m.provider || !preferredProvider || m.provider === preferredProvider)) || null;
    }
    if (match) {
      modelSel.value = match.provider ? `${match.provider}\u0000${match.id}` : match.id;
    } else if (preferredModel) {
      modelSel.value = OTHER;
      modelManual.value = preferredModel;
    } else {
      modelSel.value = models.length ? modelSel.options[0].value : OTHER;
    }
    modelManualField.hidden = modelSel.value !== OTHER;
  }

  function currentModel() {
    const mod = selectedModule();
    if (modelSel.value === OTHER || !mod) return null;
    const opt = modelSel.selectedOptions[0];
    const id = opt ? opt.getAttribute("data-id") : null;
    return modelsOf(mod).find((m) => (m.provider ? `${m.provider}\u0000${m.id}` : m.id) === modelSel.value && m.id === id) || null;
  }

  function currentModelId() {
    if (modelSel.value === OTHER) return modelManual.value.trim();
    const m = currentModel();
    return m ? m.id : "";
  }

  // 選定模型:能力帶入 capabilities、model_name 預設為 id;「其他」時能力欄位清空讓使用者填
  function applyModel() {
    modelManualField.hidden = modelSel.value !== OTHER;
    const m = currentModel();
    if (m) {
      setCaps(m.capabilities || {});
      if (isFallback(selectedModule())) llmProviderInput.value = m.provider || providerSel.value;
    } else {
      setCaps({});
      if (isFallback(selectedModule()) && !llmProviderInput.value) llmProviderInput.value = providerSel.value;
    }
    if (state.modelNameAuto) modelNameInput.value = currentModelId();
  }

  function setKind(kind) {
    state.kind = kind;
    for (const b of kindButtons) {
      const on = b.getAttribute("data-kind") === kind;
      b.setAttribute("aria-pressed", on ? "true" : "false");
      b.classList.toggle("on", on);
    }
    syncKindFields();
    if (state.modules) {
      renderProviders(null);
      llmProviderInput.value = "";
      renderModels(null, null);
      applyModel();
    }
  }

  function showFormError(text, el) {
    formError.hidden = false;
    setText(formError, text);
    for (const x of form.querySelectorAll("[aria-invalid]")) x.removeAttribute("aria-invalid");
    if (el) {
      el.setAttribute("aria-invalid", "true");
      el.focus();
    }
  }

  function clearFormError() {
    formError.hidden = true;
    setText(formError, "");
    for (const x of form.querySelectorAll("[aria-invalid]")) x.removeAttribute("aria-invalid");
  }

  function parseParams() {
    let v;
    try {
      v = JSON.parse(paramsInput.value.trim() || "{}");
    } catch {
      return null;
    }
    return v && typeof v === "object" && !Array.isArray(v) ? v : null;
  }

  // ---- 事件 ----
  for (const b of kindButtons) b.addEventListener("click", () => setKind(b.getAttribute("data-kind")));
  providerSel.addEventListener("change", () => {
    updateProviderNote();
    llmProviderInput.value = "";
    renderModels(null, null);
    applyModel();
  });
  modelSel.addEventListener("change", applyModel);
  modelManual.addEventListener("input", () => {
    if (state.modelNameAuto) modelNameInput.value = modelManual.value.trim();
  });
  modelNameInput.addEventListener("input", () => {
    state.modelNameAuto = modelNameInput.value === "";
  });
  // 上游網址 ⇄ params.api_base
  upstreamInput.addEventListener("input", () => {
    const p = parseParams();
    if (!p) return;
    const v = upstreamInput.value.trim();
    if (v) p.api_base = v;
    else delete p.api_base;
    setParamsText(p);
  });
  paramsInput.addEventListener("input", () => {
    const p = parseParams();
    if (p && typeof p.api_base === "string") upstreamInput.value = p.api_base;
    else if (p && !("api_base" in p)) upstreamInput.value = "";
  });
  budgetType.addEventListener("change", syncBudget);
  windowSel.addEventListener("change", syncBudget);
  tagInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      addTag();
    }
  });
  tagAdd.addEventListener("click", addTag);
  if (cancelBtn) cancelBtn.addEventListener("click", () => onCancel());

  function buildBody() {
    clearFormError();
    const id = editing ? initial.id : idInput.value.trim();
    if (!id || /[\s/]/.test(id)) return showFormError(S.err_id, idInput);
    const opt = providerSel.selectedOptions[0];
    if (!state.modules || !opt || !opt.value || (opt.disabled && !(editing && opt.value === initial.provider))) {
      return showFormError(S.err_provider, providerSel);
    }
    const mod = selectedModule();
    const provider = isFallback(mod) && llmProviderInput.value.trim() ? llmProviderInput.value.trim() : opt.value;
    const model = currentModelId();
    if (!model) return showFormError(S.err_model, modelSel.value === OTHER ? modelManual : modelSel);
    const modelName = modelNameInput.value.trim();
    if (!modelName) return showFormError(S.err_model_name, modelNameInput);
    const params = parseParams();
    if (!params) return showFormError(S.err_params, paramsInput);
    let note = null;
    if (state.kind === "api" || state.kind === "local") {
      const up = upstreamInput.value.trim();
      if (state.kind === "local" && !up) return showFormError(S.err_upstream, upstreamInput);
      if (up) {
        if (typeof params.api_base === "string" && params.api_base !== up) note = S.sync_note;
        params.api_base = up;
      } else {
        delete params.api_base;
      }
    }
    const caps = editing ? deepCopy(initial.capabilities) || {} : {};
    const ctxText = ctxInput.value.trim();
    if (ctxText) {
      const n = Number(ctxText);
      if (!Number.isInteger(n) || n < 1) return showFormError(S.err_ctx, ctxInput);
      caps.ctx = n;
    } else {
      delete caps.ctx;
    }
    caps.tools = toolsBox.checked;
    caps.vision = visionBox.checked;
    caps.efforts = effortBoxes.filter((b) => b.checked).map((b) => b.value);
    let budget = null;
    if (budgetType.value !== "none") {
      const lt = limitInput.value.trim();
      const limit = lt === "" ? null : Number(lt);
      if (limit !== null && (!Number.isFinite(limit) || limit < 0)) return showFormError(S.err_limit, limitInput);
      let anchor = null;
      if (windowSel.value === "monthly" && anchorInput.value.trim() !== "") {
        anchor = Number(anchorInput.value.trim());
        if (!Number.isInteger(anchor) || anchor < 1 || anchor > 28) return showFormError(S.err_anchor, anchorInput);
      }
      budget = { type: budgetType.value, limit_amount: limit, window: windowSel.value, reset_anchor: anchor };
    }
    addTag();
    if (requireTag && !state.tags.length) return showFormError(S.err_tag, tagInput);
    const cred = credInput.value.trim();
    return {
      body: {
        id, model_name: modelName, kind: state.kind, provider, model, params,
        credential_ref: cred || null, capabilities: caps, enabled: enabledBox.checked,
        tags: [...state.tags], budget,
      },
      note,
    };
  }

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const built = buildBody();
    if (!built) return;
    if (built.note) {
      upstreamInput.dispatchEvent(new Event("input"));
    }
    await withBusy(submitBtn, async () => {
      try {
        const saved = editing
          ? await sendJSON("PUT", `/api/deployments/${encodeURIComponent(initial.id)}`, built.body)
          : await sendJSON("POST", "/api/deployments", built.body);
        if (state.destroyed) return;
        showMessage(built.note ? `${S.saved}(${built.note})` : S.saved, built.note ? "warn" : "ok");
        if (onSaved) await onSaved(saved, { created: !editing });
      } catch (err) {
        if (!state.destroyed) showError(err);
      }
    });
  });

  container.append(form);

  // ---- 載入 adapters ----
  (async () => {
    try {
      const data = await getJSON("/api/adapters");
      if (state.destroyed) return;
      state.modules = data.modules || [];
      renderProviders(base.provider || null);
      const mod = selectedModule();
      if (isFallback(mod) && base.provider && base.provider !== providerSel.value) llmProviderInput.value = base.provider;
      renderModels(base.model || null, isFallback(mod) ? base.provider : null);
      if (editing) {
        if (isFallback(mod) && !llmProviderInput.value) llmProviderInput.value = base.provider;
      } else {
        const keepCaps = Object.keys(base.capabilities || {}).length > 0;
        if (keepCaps) {
          if (state.modelNameAuto) modelNameInput.value = currentModelId();
        } else {
          applyModel();
        }
      }
      submitBtn.disabled = false;
    } catch (err) {
      if (state.destroyed) return;
      clear(providerSel);
      providerSel.append(h("option", { value: "" }, TEXT.http_error));
      showError(err);
    }
  })();

  return {
    element: form,
    destroy() {
      state.destroyed = true;
      form.remove();
    },
  };
}


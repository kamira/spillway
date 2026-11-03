// DOM 建構(CHG-20261103-01 決策 8;AM-12)。
// 動態資料只以 text node、value 或 setAttribute 放入 DOM;不以字串解析 HTML。
// h() 在執行期拒絕:事件屬性(on*)、style、srcdoc、formaction;href/src/action 的值不以
// ./、/ui/、/api/、#、?、blob: 開頭;危險的 tag(denylist)。

const BANNED_TAGS = new Set(["script", "style", "iframe", "object", "embed", "base", "link", "meta",
  "frame", "frameset"]);
const BANNED_ATTRS = new Set(["style", "srcdoc", "formaction"]);
const URL_ATTRS = new Set(["href", "src", "action", "xlink:href"]);
const SAFE_URL = /^(\.\/|\/ui\/|\/api\/|#|\?|blob:)/;

export class DomError extends Error {
  constructor(message) {
    super(message);
    this.name = "DomError";
  }
}

export function isSafeUrl(value) {
  return SAFE_URL.test(String(value));
}

export function setAttr(el, name, value) {
  const n = String(name).toLowerCase();
  if (/^on/i.test(n) || BANNED_ATTRS.has(n)) throw new DomError(`不允許的屬性:${name}`);
  if (URL_ATTRS.has(n) && !isSafeUrl(value)) throw new DomError(`不允許的網址:${name}`);
  el.setAttribute(name, value === true ? "" : String(value));
  return el;
}

export function append(parent, ...children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) {
      append(parent, ...child);
    } else if (child instanceof Node) {
      parent.appendChild(child);
    } else if (typeof child === "string" || typeof child === "number" || typeof child === "bigint") {
      parent.appendChild(document.createTextNode(String(child)));
    } else {
      throw new DomError("不支援的子節點型別");
    }
  }
  return parent;
}

// h(tag, attrs, ...children):字串子節點一律成為 text node。
// attrs 的值為 null、undefined、false 時略過,true 時設為空字串(布林屬性)。
export function h(tag, attrs = {}, ...children) {
  const t = String(tag).toLowerCase();
  if (!/^[a-z][a-z0-9-]*$/.test(t) || BANNED_TAGS.has(t)) throw new DomError(`不允許的 tag:${tag}`);
  const el = document.createElement(t);
  for (const [name, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    setAttr(el, name, value);
  }
  return append(el, ...children);
}

export function byId(id) {
  return document.getElementById(id);
}

export function clear(el) {
  el.replaceChildren();
  return el;
}

export function setText(el, text) {
  el.textContent = text === null || text === undefined ? "" : String(text);
  return el;
}

// 以資料值查找元素時一律經 CSS.escape
export function queryData(root, name, value) {
  return root.querySelector(`[data-${name}="${CSS.escape(String(value))}"]`);
}

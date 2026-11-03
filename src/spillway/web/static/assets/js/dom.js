// DOM 建構(CHG-20261103-01 決策 8;AM-12)。
// 動態資料只以 text node、value 或 setAttribute 放入 DOM;不以字串解析 HTML。
// h() 在執行期拒絕:事件屬性(on*)、style、srcdoc、formaction;網址屬性(href、src、action、
// srcset 的每個候選、poster、ping 的每個網址)不以 ./、/ui/、/api/、#、?、blob: 開頭;危險的 tag(denylist)。
// 屬性值只轉成字串一次,檢查與寫入用同一個值。

const BANNED_TAGS = new Set(["script", "style", "iframe", "object", "embed", "base", "link", "meta",
  "frame", "frameset"]);
const BANNED_ATTRS = new Set(["style", "srcdoc", "formaction"]);
const URL_ATTRS = new Set(["href", "src", "action", "xlink:href", "poster"]);
// 值內含多個網址的屬性:srcset 以逗號分隔(每個候選取第一段),ping 以空白分隔
const MULTI_URL_ATTRS = new Map([
  ["srcset", (v) => v.split(",").map((c) => c.trim().split(/\s+/)[0])],
  ["ping", (v) => v.trim().split(/\s+/)],
]);
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
  const v = value === true ? "" : String(value);
  if (URL_ATTRS.has(n) && !isSafeUrl(v)) throw new DomError(`不允許的網址:${name}`);
  const split = MULTI_URL_ATTRS.get(n);
  if (split && !split(v).every((u) => isSafeUrl(u))) throw new DomError(`不允許的網址:${name}`);
  el.setAttribute(name, v);
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

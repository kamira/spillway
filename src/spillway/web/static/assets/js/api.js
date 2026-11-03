// 同源 /api 呼叫(CHG-20261103-01 決策 20;AM-2、AM-12)。
// 一律帶 cache: "no-store";非 2xx 時解析 spec 9.1.2 為 ApiError{status, code, message},
// 非 JSON 時以狀態碼表示;2xx 但 body 無法解析時為 ApiError{code: "bad_response"};fetch 本身失敗時丟 ApiError{status: 0, code: "network_error"}
// (只用於前端,不是伺服端錯誤碼)。不輸出請求或回應內容到 console。

import { TEXT } from "./strings.js";

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

async function toApiError(resp) {
  const fallback = new ApiError(resp.status, "http_error", `${TEXT.http_error}(HTTP ${resp.status})`);
  let body;
  try {
    body = await resp.json();
  } catch {
    return fallback;
  }
  const err = body && typeof body === "object" ? body.error : null;
  if (err && typeof err.code === "string" && typeof err.message === "string") {
    return new ApiError(resp.status, err.code, err.message);
  }
  return fallback;
}

export async function request(method, path, { body, headers } = {}) {
  let resp;
  try {
    resp = await fetch(path, {
      method,
      headers,
      body,
      cache: "no-store",
      credentials: "same-origin",
    });
  } catch {
    throw new ApiError(0, "network_error", TEXT.network_error);
  }
  if (!resp.ok) throw await toApiError(resp);
  return resp;
}

// 2xx 但 body 無法解析(非 JSON、讀取中斷)時也丟 ApiError,畫面不會顯示引擎的英文訊息
async function readBody(resp, read) {
  try {
    return await read();
  } catch {
    throw new ApiError(resp.status, "bad_response", TEXT.bad_response);
  }
}

async function parseBody(resp) {
  if (resp.status === 204) return null;
  const type = resp.headers.get("content-type") || "";
  if (type.includes("application/json")) return readBody(resp, () => resp.json());
  return readBody(resp, () => resp.text());
}

export async function getJSON(path) {
  const resp = await request("GET", path, { headers: { Accept: "application/json" } });
  return readBody(resp, () => resp.json());
}

export async function sendJSON(method, path, body) {
  const init = { headers: { Accept: "application/json" } };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  return parseBody(await request(method, path, init));
}

export async function sendText(method, path, text, contentType = "text/plain;charset=utf-8") {
  const resp = await request(method, path, {
    headers: { Accept: "application/json", "Content-Type": contentType },
    body: text,
  });
  return parseBody(resp);
}

// 組 query:一律經 URLSearchParams 編碼;值為 null、undefined、"" 時略過
export function withQuery(path, params) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v === null || v === undefined || v === "") continue;
    q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `${path}?${s}` : path;
}

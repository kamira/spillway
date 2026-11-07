"""UI-7 設定與首次設定精靈的頁面操作(CHG-20261103-01 T8;behave 步驟可直接匯入)。

(CHG-20261107-03 T2 由 `tests/helpers/ui_settings.py` 複製改寫成黑箱版;原檔不動,T5 改用本模組後刪除)
只用 Playwright **sync** API 與同步 `gw.http()`(經 `tests.blackbox.ui.api`),不在主 thread 使用 asyncio。

- `open_settings(page, gw, hash_="")`:開啟 `settings.html`,等總覽與 context 表單載入完成。
- `open_wizard(page, gw)`:開啟 `wizard.html`,等到某一步出現(`main[data-step]`)。
- `wizard_step(page) -> int`:目前步驟。
- `import_requests(guard) -> list[str]`:guard 記錄的 `POST /api/config/import` 網址。
- `choose_import(page, data, *, mode="merge", name="spillway.yaml")`:選檔(bytes)並選 mode。
- `import_dialog(page) -> Locator`:匯入確認對話框(`confirmDialog`)。
- `export_yaml(gw) -> str`:`GET /api/config/export` 的內容。
- `token_hash(gw) -> str | None`:資料庫中的存取 token 雜湊(唯讀連線讀 `settings` 的字面鍵
  `access_token_sha256`;AM-9)。
- `page_leaks(page, secret) -> list[str]`:`document.documentElement.outerHTML`、全部 `input.value`/
  `textarea.value`、`localStorage`、`sessionStorage`、`location.href` 中含 `secret` 的位置(空清單 = 乾淨)。
"""
from __future__ import annotations

from typing import Any

from playwright.sync_api import Locator, Page, expect

from tests.blackbox.db import ACCESS_TOKEN_KEY, setting
from tests.blackbox.ui import api, goto, wait_loaded

__all__ = ["choose_import", "export_yaml", "import_dialog", "import_requests", "open_settings", "open_wizard",
           "page_leaks", "token_hash", "wizard_step"]


def open_settings(page: Page, gw: Any, hash_: str = "") -> None:
    goto(page, gw, "/ui/settings.html" + hash_)
    wait_loaded(page, "#overview")
    wait_loaded(page, "#context-form")


def open_wizard(page: Page, gw: Any) -> None:
    goto(page, gw, "/ui/wizard.html")
    expect(page.locator("main[data-step]")).to_have_count(1)


def wizard_step(page: Page) -> int:
    return int(page.locator("main").get_attribute("data-step") or "0")


def import_requests(guard: Any) -> list[str]:
    return [u for m, u in guard.requests if m == "POST" and "/api/config/import" in u]


def choose_import(page: Page, data: bytes, *, mode: str = "merge", name: str = "spillway.yaml") -> None:
    page.locator("#import-file").set_input_files({"name": name, "mimeType": "application/yaml", "buffer": data})
    page.locator(f'input[name="import_mode"][value="{mode}"]').check()


def import_dialog(page: Page) -> Locator:
    return page.locator("dialog").filter(has=page.locator('[data-role="import-preview"]'))


def export_yaml(gw: Any) -> str:
    return api(gw, "GET", "/api/config/export").text


def token_hash(gw: Any) -> str | None:
    with gw.db() as conn:
        return setting(conn, ACCESS_TOKEN_KEY)


def page_leaks(page: Page, secret: str) -> list[str]:
    return page.evaluate(
        """(secret) => {
             const hits = [];
             if (document.documentElement.outerHTML.includes(secret)) hits.push("outerHTML");
             for (const el of document.querySelectorAll("input, textarea")) {
               if (String(el.value).includes(secret)) hits.push(`value:${el.id || el.name || el.tagName}`);
             }
             const stores = [["localStorage", window.localStorage], ["sessionStorage", window.sessionStorage]];
             for (const [name, store] of stores) {
               for (let i = 0; i < store.length; i++) {
                 const k = store.key(i);
                 if (String(k).includes(secret) || String(store.getItem(k)).includes(secret)) hits.push(name);
               }
             }
             if (window.location.href.includes(secret)) hits.push("location.href");
             return hits;
           }""", secret)

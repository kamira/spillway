"""UI-5 MCP 設定與 UI-8 API 參考的頁面操作 helper(CHG-20261103-01 T7;決策 15、19、22)。

只用 Playwright **sync** API 與同步 httpx(`gw.http()`);T9 的 behave 步驟可直接匯入。

- 群組以 `tests.helpers.ui.seed_group` 建立。
- `mcp_config(gw, group, tool)`、`mcp_catalog(gw, group="all")`:同步讀取伺服端的期望值。
- `open_mcp(page, gw, group="all", tool="claude-code")`:開啟 UI-5 並等到該組合載入完成。
- `select_mcp(page, *, group=None, tool=None)`:切換群組或工具分頁並等待載入完成。
- `mcp_variants(page) -> list[dict]`:畫面上每個 variant 的 label、scope、path 文字、filename、content。
- `open_reference(page, gw, query="", hash_="")`:開啟 UI-8 並等到 catalog 載入完成(或錯誤)。
- `BLOB_SPY`:`page.add_init_script` 用的腳本,記錄 `URL.createObjectURL` 的 Blob type 與 `revokeObjectURL`
  (`window.__blobs`、`window.__revoked`)。
- `DENY_CLIPBOARD`:`page.add_init_script` 用的腳本,讓 `navigator.clipboard.writeText` 以 `NotAllowedError`
  拒絕(模擬剪貼簿權限被拒)。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from playwright.sync_api import Page

from tests.helpers.ui import goto

__all__ = ["BLOB_SPY", "DENY_CLIPBOARD", "TOOLS", "mcp_catalog", "mcp_config", "mcp_variants", "open_mcp",
           "open_reference", "select_mcp"]

TOOLS = ("claude-code", "codex", "cursor", "gemini", "stdio")

BLOB_SPY = """
(() => {
  const create = URL.createObjectURL.bind(URL);
  const revoke = URL.revokeObjectURL.bind(URL);
  window.__blobs = [];
  window.__revoked = [];
  URL.createObjectURL = (obj) => {
    const url = create(obj);
    window.__blobs.push({ url, type: obj && obj.type });
    return url;
  };
  URL.revokeObjectURL = (url) => { window.__revoked.push(url); return revoke(url); };
})();
"""

DENY_CLIPBOARD = """
(() => {
  const denied = {
    writeText: () => Promise.reject(new DOMException("Write permission denied.", "NotAllowedError")),
    readText: () => Promise.reject(new DOMException("Read permission denied.", "NotAllowedError")),
  };
  Object.defineProperty(Navigator.prototype, "clipboard", { configurable: true, get: () => denied });
})();
"""


def mcp_config(gw: Any, group: str, tool: str) -> dict[str, Any]:
    with gw.http() as client:
        resp = client.get("/api/mcp/config", params={"group": group, "tool": tool})
    assert resp.status_code == 200, resp.text
    return resp.json()


def mcp_catalog(gw: Any, group: str = "all") -> dict[str, Any]:
    with gw.http() as client:
        resp = client.get("/api/mcp/catalog", params={"group": group})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _wait_mcp(page: Page, group: str, tool: str) -> None:
    """等到該組合載入完成;載入失敗(`data-mcp-ready="error"`)時立即失敗,不等逾時。"""
    page.wait_for_selector(f'html[data-mcp-ready="{group}|{tool}"], html[data-mcp-ready="error"]', state="attached")
    state = page.locator("html").get_attribute("data-mcp-ready")
    assert state == f"{group}|{tool}", f"UI-5 載入失敗:{state}"


def open_mcp(page: Page, gw: Any, group: str = "all", tool: str = "claude-code") -> None:
    goto(page, gw, f"/ui/mcp.html?group={quote(group, safe='')}&tool={quote(tool, safe='')}")
    _wait_mcp(page, group, tool)


def select_mcp(page: Page, *, group: str | None = None, tool: str | None = None) -> None:
    cur_group, cur_tool = (page.locator("html").get_attribute("data-mcp-ready") or "|").split("|", 1)
    if group is not None and group != cur_group:
        page.select_option("#mcp-group", group)
        cur_group = group
        _wait_mcp(page, cur_group, cur_tool)
    if tool is not None and tool != cur_tool:
        page.click(f'#mcp-tools [data-tool="{tool}"]')
        cur_tool = tool
        _wait_mcp(page, cur_group, cur_tool)


def mcp_variants(page: Page) -> list[dict[str, Any]]:
    return page.eval_on_selector_all(
        "#mcp-variants .mcp-variant",
        """els => els.map(el => {
             const q = (r) => el.querySelector(`[data-role="${r}"]`);
             return {
               label: q("label").textContent,
               scope: q("scope").getAttribute("title"),
               path: q("path").textContent,
               filename: q("filename") ? q("filename").textContent : null,
               content: q("content").textContent,
               download: !!q("download"),
             };
           })""")


def open_reference(page: Page, gw: Any, query: str = "", hash_: str = "") -> None:
    goto(page, gw, f"/ui/api-reference.html{query}{hash_}")
    page.wait_for_selector("html[data-ref-ready]", state="attached")

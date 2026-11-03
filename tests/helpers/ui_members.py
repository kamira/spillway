"""UI-2 成員與 UI-3 群組的頁面操作與種資料(CHG-20261103-01 T5;T9 的 behave 步驟可直接匯入)。

只用 Playwright **sync** API 與同步 `gw.http()`(經 `tests.helpers.ui.api`),不在主 thread 使用 asyncio。

- `member_body(dep_id, **kw)`:`POST /api/deployments` 的 body(預設 openai api 成員,憑證 `env:SPILLWAY_TEST_KEY`)。
- `seed_member(gw, dep_id, **kw) -> dict`、`seed_group(gw, name, selector)`、`seed_route(gw, name, target)`。
- `broken_adapter_dir(root) -> Path`:在 `root` 寫出無法 import 的模組 `broken`(provider `broken-p`;
  同 `tests/integration/admin/test_admin_adapters.py` 的 `pre_import="import not_a_real_pkg_xyz\\n"`)。
- `MemberForm(page)`:成員表單的定位器與常用操作(類型、供應商、模型、欄位、送出)。
- `open_members(page, gw, member_id=None)`、`open_groups(page, gw)`。
- `requests_of(guard, method, path_part)`:guard 記錄的請求中方法與路徑相符者。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from playwright.sync_api import Locator, Page

from tests.contract.fakemods import write_module
from tests.helpers import TEST_KEY_ENV
from tests.helpers.ui import api, goto

__all__ = ["MemberForm", "broken_adapter_dir", "member_body", "open_groups", "open_members", "requests_of",
           "seed_group", "seed_member", "seed_route"]

BROKEN_PROVIDER = "broken-p"


def member_body(dep_id: str, **kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": dep_id, "model_name": dep_id, "kind": "api", "provider": "openai", "model": "gpt-5.5",
        "params": {"api_base": "http://127.0.0.1:9/v1"}, "credential_ref": f"env:{TEST_KEY_ENV}",
        "capabilities": {"ctx": 1000, "tools": True, "vision": False, "efforts": []},
        "enabled": True, "tags": ["chat"], "budget": None,
    }
    body.update(kw)
    return body


def seed_member(gw: Any, dep_id: str, **kw: Any) -> dict[str, Any]:
    return api(gw, "POST", "/api/deployments", member_body(dep_id, **kw)).json()


def seed_group(gw: Any, name: str, selector: dict[str, list[str]]) -> dict[str, Any]:
    return api(gw, "POST", "/api/groups", {"name": name, "selector": selector}).json()


def seed_route(gw: Any, name: str, target: dict[str, Any], mode: str = "chat") -> dict[str, Any]:
    return api(gw, "POST", "/api/routes", {"name": name, "target": target, "mode": mode}).json()


def broken_adapter_dir(root: Path) -> Path:
    write_module(root, "broken", pre_import="import not_a_real_pkg_xyz\n", providers=(BROKEN_PROVIDER,))
    return root


def open_members(page: Page, gw: Any, member_id: str | None = None) -> None:
    path = "/ui/members.html" + (f"#{member_id}" if member_id else "")
    goto(page, gw, path)


def open_groups(page: Page, gw: Any) -> None:
    goto(page, gw, "/ui/groups.html")


def requests_of(guard: Any, method: str, path_part: str) -> list[str]:
    return [u for m, u in guard.requests if m == method and path_part in u]


class MemberForm:
    """`form.member-form` 的定位器(頁面上同時只有一個表單)。"""

    def __init__(self, page: Page, root: Locator | None = None) -> None:
        self.page = page
        self.form = (root or page.locator("body")).locator("form.member-form")

    def field(self, name: str) -> Locator:
        return self.form.locator(f'[name="{name}"]')

    @property
    def provider(self) -> Locator:
        return self.field("provider")

    @property
    def model(self) -> Locator:
        return self.field("model")

    @property
    def submit(self) -> Locator:
        return self.form.locator('[data-role="submit"]')

    @property
    def error(self) -> Locator:
        return self.form.locator('[data-role="form-error"]')

    def kind(self, kind: str) -> None:
        self.form.locator(f'button[data-kind="{kind}"]').click()

    def option_values(self, select: Locator, *, enabled_only: bool = False) -> list[str]:
        sel = "option:not([disabled])" if enabled_only else "option"
        return select.locator(sel).evaluate_all("(os) => os.map((o) => o.value)")

    def option_texts(self, select: Locator) -> list[str]:
        return select.locator("option").evaluate_all("(os) => os.map((o) => o.textContent)")

    def wait_ready(self) -> None:
        from playwright.sync_api import expect
        expect(self.submit).to_be_enabled()

    def caps(self) -> dict[str, Any]:
        ctx = self.field("ctx").input_value()
        return {
            "ctx": int(ctx) if ctx else None,
            "tools": self.field("tools").is_checked(),
            "vision": self.field("vision").is_checked(),
            "efforts": self.form.locator('input[name="effort"]:checked').evaluate_all(
                "(bs) => bs.map((b) => b.value)"),
        }

    def add_tag(self, tag: str) -> None:
        self.field("tag").fill(tag)
        self.field("tag").press("Enter")

    def set_budget(self, type_: str, limit: str = "", window: str = "monthly", anchor: str = "") -> None:
        self.field("budget_type").select_option(type_)
        if type_ != "none":
            self.field("limit_amount").fill(limit)
            self.field("window").select_option(window)
            if window == "monthly":
                self.field("reset_anchor").fill(anchor)

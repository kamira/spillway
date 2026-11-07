"""M4b 操作驗收的自動部分(CHG-20261103-01「### Acceptance operation」①–⑩;T9 L4;macOS)。

驅動方式
- 子行程:假 API 上游(`python -m tests.fakes.api_upstream`)與 `SPILLWAY_CMD serve`(`tests.blackbox.harness.Serve`,
  開啟測試控制介面以種入帳本與冷卻;環境帶 `SPILLWAY_TEST_KEY=<假值>`,PATH 只含假 claude/codex)。每個資料目錄先以
  子行程 `SPILLWAY_CMD init`(`run_cli`)產生 token(serve 看到已有雜湊就不印;明文只存在測試行程記憶體 `TOKENS`)。
  成員的上游網址一律填假上游。本檔不 import spillway(CHG-20261107-03 T3;邊界守門)。
- ⑨(AC-21)以子行程 serve + `SPILLWAY_TEST_ADAPTER_PATHS`(`Serve(adapter_paths=[tmp])`;CHG-20261107-03 附錄,
  取代轉換前的行程內 `start_live_gateway(extra_adapter_paths=…)`;實作相依,見 `tests/blackbox/impl_dependent.toml`)
  啟動;模組以 `tests.blackbox.ui.members.broken_adapter_dir`
  (`fakemods.write_module(…, pre_import="import not_a_real_pkg_xyz\\n")`)建立。AC-21 第三句(`adapter_unavailable`)
  由 `m2_adapters.feature` 與 `tests/contract/test_exclude_reasons.py` 涵蓋。
- 瀏覽器:Playwright sync API、headless Chromium,module 範圍;主 thread 不用 asyncio(決策 22)。一個 context
  (1280×800、Asia/Taipei、剪貼簿權限),`tests.blackbox.ui.open_context` 攔截非本機請求並收集 console error 與
  pageerror;結束時必須為 0。頁面操作一律用 `tests/blackbox/ui/`(由 `tests/helpers/ui*.py` 複製的黑箱版)。
- 匯出 A 在 ③ 完成之後(⑦);第二個資料目錄的 serve 以畫面 replace「預覽匯入」→ 確認 → 匯出 B(AC-10)。

產物(`artifacts/m4b/`,不進版控)
- `screens/NN-<畫面>.png`:每個畫面一張 1280×800(viewport)截圖;token 對話框開啟時不截圖。
- `operation-results.json`:各步驟的觀察值(不含任何秘密)。
- `ac8-timing.json`:① 從開啟 `/` 到範例呼叫 200 的耗時(自動化只是回歸護欄,AC-8 以擁有者步驟 ④ 計時)。
NFR-4:以假金鑰與全部 token 明文搜尋資料庫(含 -wal/-shm)、設定 YAML A 與 B、帳本 CSV、第二資料目錄的
`backups/`、serve 日誌(遮罩副本與原始 stderr)、全部畫面的 HTML 與畫面期間的 `/api/` 回應,以及 `artifacts/m4b/`
的文字檔,命中須為 0。
"""
from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import re
import sqlite3
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
import pytest
import yaml
from playwright.sync_api import Browser, Error, Page, Playwright, Response, expect, sync_playwright

from tests.acceptance import procs
from tests.blackbox.control import Control
from tests.blackbox.harness import Serve, child_env, run_cli
from tests.blackbox.mask import redact_tokens
from tests.blackbox.ui import (CLIP, NAV, close_and_collect, confirm_dialog, db_snapshot, goto, open_context,
                               wait_loaded)
from tests.blackbox.ui.ledger import insert_ledger
from tests.blackbox.ui.mcp import TOOLS, mcp_config, mcp_variants, open_mcp, open_reference
from tests.blackbox.ui.members import BROKEN_MODULE, BROKEN_PROVIDER, MemberForm, broken_adapter_dir, open_members
from tests.blackbox.ui.overview import set_cooldown
from tests.blackbox.ui.routes import drag_slot, editor_slots, open_editor, route_order
from tests.blackbox.ui.settings import choose_import, import_dialog, import_requests, open_settings, page_leaks
from tests.fakes.cli_upstream import assert_only_fakes, install

pytestmark = pytest.mark.skipif(not procs.HAS_LSOF, reason="需要 lsof(macOS 驗收平台)")

REPO_ROOT = procs.REPO_ROOT
ARTIFACTS = REPO_ROOT / "artifacts" / "m4b"
SCREENS = ARTIFACTS / "screens"
RESULTS = ARTIFACTS / "operation-results.json"
AC8 = ARTIFACTS / "ac8-timing.json"
FAKE_KEY = procs.TEST_KEY                 # 子行程 SPILLWAY_TEST_KEY 的假值
CRED_REF = "env:SPILLWAY_TEST_KEY"
CLAUDE_MODELS = REPO_ROOT / "src" / "spillway" / "adapters" / "claude_cli" / "models.yaml"
OTHER_TEXT = "其他(手動輸入)"
TPE = ZoneInfo("Asia/Taipei")
GRACE = 2.0
AC8_LIMIT_S = 600.0
ROTATE = "/api/settings/token/rotate"
TOKENS: list[str] = []                    # 本模組產生過的全部 token 明文(只在測試行程記憶體)


# ---------------------------------------------------------------- Playwright(module 範圍;決策 22)

@pytest.fixture(scope="module", autouse=True)
def _no_event_loop_leak() -> Iterator[None]:
    yield
    assert asyncio._get_running_loop() is None, "Playwright 的事件迴圈標記外洩到主 thread"


@pytest.fixture(scope="module")
def playwright() -> Iterator[Playwright]:
    pw = sync_playwright().start()
    try:
        yield pw
    finally:
        pw.stop()


@pytest.fixture(scope="module")
def browser(playwright: Playwright) -> Iterator[Browser]:
    b = playwright.chromium.launch(headless=True)
    try:
        yield b
    finally:
        b.close()


# ---------------------------------------------------------------- 產物

def record(key: str, value: Any) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(RESULTS.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        data = {}
    data.update({"platform": "macOS、Chromium 1248 headless(Linux、Windows 與其他瀏覽器未涵蓋)", key: value})
    RESULTS.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _safe(text: str) -> str:
    return redact_tokens(redact_tokens(text, TOKENS), FAKE_KEY)


@pytest.fixture(scope="module", autouse=True)
def _fresh_artifacts() -> Iterator[None]:
    SCREENS.mkdir(parents=True, exist_ok=True)
    for p in (RESULTS, AC8, *SCREENS.glob("*.png")):
        p.unlink(missing_ok=True)
    TOKENS.clear()
    yield
    secrets = [FAKE_KEY, *TOKENS]
    hits = [str(p) for p in ARTIFACTS.rglob("*") if p.is_file() and p.suffix in (".json", ".xml", ".txt")
            and any(s.encode() in p.read_bytes() for s in secrets)]
    assert hits == [], f"artifacts/m4b 含秘密:{hits}"


# ---------------------------------------------------------------- 子行程 serve 與 helper 轉接

class ServeGw:
    """子行程 serve(`tests.blackbox.harness.Serve`)的轉接:提供 `tests/blackbox/ui/` 需要的 `url`、`http`、`db`
    (唯讀)、`control`(`/__test__/*`)、`port`、`db_path`。`adapter_paths` 給了就以 `SPILLWAY_TEST_ADAPTER_PATHS`
    載入(`allowed_unavailable` 為允許載入失敗的模組名)。"""

    def __init__(self, tmp: Path, name: str, fake_bin: Path, serves: list[Serve], *,
                 adapter_paths: list[Path] | None = None, allowed_unavailable: tuple[str, ...] = ()) -> None:
        self.name = name
        self.home = tmp / name
        self.home.mkdir()
        self.extra_env = {"SPILLWAY_TEST_KEY": FAKE_KEY}
        self.token = self._init_token(fake_bin)
        self.serve = Serve(self.home, fake_bin=fake_bin, graceful_timeout=GRACE, log_path=tmp / f"serve-{name}.log",
                           extra_env=self.extra_env, adapter_paths=adapter_paths or (),
                           allowed_unavailable_adapters=allowed_unavailable)
        serves.append(self.serve)
        self.serve.start()
        self.port = self.serve.port
        self.base_url = self.serve.base_url
        self.db_path = self.serve.db_path

    @property
    def control(self) -> Control:
        return self.serve.control

    def stop(self) -> None:
        self.serve.stop()

    def _init_token(self, fake_bin: Path) -> str:
        r = run_cli("init", home=self.home, fake_bin=fake_bin, extra_env=self.extra_env, timeout=120)
        token = r.stdout.strip()
        if token:
            TOKENS.append(token)
        assert r.returncode == 0 and token and len(r.stdout.splitlines()) == 1, f"init 失敗 rc={r.returncode}"
        return token

    def url(self, path: str) -> str:
        return self.base_url + path

    def http(self, **kw: Any) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, trust_env=False, timeout=60, **kw)

    @contextlib.contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        with self.serve.db() as conn:          # 唯讀(mode=ro)
            conn.execute("PRAGMA busy_timeout = 30000")
            yield conn

    def get(self, path: str, **kw: Any) -> Any:
        with self.http() as c:
            r = c.get(path, **kw)
        assert r.status_code == 200, f"GET {path} → {r.status_code} {_safe(r.text)}"
        return r.json()


class Screens:
    """截圖與畫面期間的 HTML、`/api/` 回應收集(NFR-4)。"""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.n = 0
        self.shots: list[str] = []
        self.captured: list[tuple[str, bytes]] = []       # (來源, 內容)
        self.unreadable: list[str] = []
        self.rotations = 0
        page.on("response", self._on_response)

    def close(self) -> None:
        self.page.remove_listener("response", self._on_response)

    def _on_response(self, r: Response) -> None:
        # 回應到達時立即在 callback 內讀 body。導覽競態(回應到達時頁面已開始換頁)會讓 body 無法取得;
        # 這些記到 unreadable,結尾斷言同一 method+path 至少有一次被讀到並搜尋過。
        if "/api/" not in r.url:
            return
        key = f"api:{r.request.method} {r.url}"
        if r.url.endswith(ROTATE):        # 唯一允許含 token 的回應(只在對話框顯示一次),不列入搜尋
            self.rotations += 1
            return
        try:
            self.captured.append((key, r.body()))
        except Error:
            self.unreadable.append(f"{r.request.method} {r.url.split('?')[0]}")

    def html(self, label: str) -> None:
        self.captured.append((f"html:{label}", self.page.content().encode("utf-8")))

    def shot(self, label: str) -> None:
        """目前畫面:收 HTML 與回應、截圖(viewport 1280×800)。token 對話框開啟時拒絕。"""
        assert self.page.locator("#token-dialog").count() == 0, "token 對話框開啟時不可截圖"
        self.page.wait_for_load_state("networkidle")
        self.html(label)
        self.n += 1
        name = f"{self.n:02d}-{label}.png"
        self.page.screenshot(path=str(SCREENS / name))
        self.shots.append(name)


def _check_disabled_providers(form: MemberForm, gw: Any) -> list[str]:
    """供應商選單中停用的選項只能是不可用模組,或已達上限的 single 模組(spec 10.2)。"""
    mods = gw.get("/api/adapters")["modules"]
    allowed = {p for m in mods if not m["available"] or (m.get("cardinality") == "single"
                                                        and m.get("existing_members", 0) > 0)
               for p in m["providers"]}
    disabled = form.provider.locator("option[disabled]").evaluate_all("(os) => os.map((o) => o.value)")
    assert set(disabled) <= allowed, (disabled, sorted(allowed))
    return disabled


def _expected_variants(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"label": v["label"], "scope": v["scope"],
             "path": v["path"] if v["path"] is not None else "在終端機執行",
             "filename": v["filename"], "content": v["content"], "download": v["filename"] is not None}
            for v in cfg["variants"]]


def _local(dt: datetime) -> str:
    return dt.astimezone(TPE).strftime("%Y/%m/%d %H:%M:%S")


def _task(gw: ServeGw, route: str, i: int) -> httpx.Response:
    with gw.http() as c:
        return c.post("/v1/chat/completions", json={
            "model": route, "metadata": {"mode": "task"},
            "messages": [{"role": "user", "content": f"m4b-operation-{i}"}]})


def _wait_step(page: Page, n: int) -> None:
    expect(page.locator(f'main[data-step="{n}"]')).to_have_count(1)
    expect(page.locator(f"#step-{n}")).to_be_visible()


def _new_member(page: Page) -> MemberForm:
    page.locator("#new-member").click()
    form = MemberForm(page)
    form.wait_ready()
    return form


def _submit_member(page: Page, form: MemberForm) -> None:
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/deployments")) as rr:
        form.submit.click()
    assert rr.value.status == 201, _safe(rr.value.text())


def _download(page: Page, locator: str, dest: Path) -> bytes:
    with page.expect_download() as dl:
        page.locator(locator).click()
    data = Path(dl.value.path()).read_bytes()
    dest.write_bytes(data)
    return data


# ---------------------------------------------------------------- 本體

@pytest.fixture
def env(tmp_path: Path) -> Iterator[dict[str, Any]]:
    fake_bin = install(tmp_path / "fake-cli-bin")
    envv = child_env(tmp_path, fake_bin=fake_bin)
    assert set(assert_only_fakes(envv["PATH"])) == {"claude", "codex"}
    fake_proc, fake_base = procs.start_fake_upstream(tmp_path)
    serves: list[Serve] = []
    errors: list[BaseException] = []
    try:
        yield {"tmp": tmp_path, "fake_bin": fake_bin, "fake_base": fake_base, "serves": serves}
    finally:
        for s in serves:                   # stop():結束碼、stderr 沒有 Traceback/ERROR、停機沒有逾時(冪等)
            try:
                s.stop()
            except BaseException as exc:  # noqa: BLE001 — 全部停完再回報
                s.kill()
                errors.append(exc)
        procs.stop(fake_proc)
    if errors:
        raise errors[0]
    assert all(s.returncode is not None for s in serves) and fake_proc.poll() is not None


def test_m4b_operation(env: dict[str, Any], browser: Browser) -> None:  # noqa: PLR0915 — 一次走完 ①–⑩
    tmp, fake_base = env["tmp"], env["fake_base"]
    upstream = f"{fake_base}/v1"
    obs: dict[str, Any] = {}
    ctx, guard = open_context(browser, permissions=CLIP)
    opened = [(ctx, guard)]
    page = ctx.new_page()
    screens = Screens(page)
    try:
        a = ServeGw(tmp, "a", env["fake_bin"], env["serves"])
        obs["listen_a"] = procs.listening(a.port)
        assert obs["listen_a"] and all(x.startswith("127.0.0.1:") for x in obs["listen_a"])

        # ① 空資料庫 → 精靈:加入 api 成員(env:SPILLWAY_TEST_KEY)、測試呼叫、建立 route、範例呼叫(AC-8)
        t0 = time.monotonic()
        page.goto(a.url("/"))
        expect(page).to_have_url(a.url("/ui/wizard.html"))
        page.wait_for_selector('html[data-shell="ready"]', state="attached")
        _wait_step(page, 1)
        form = MemberForm(page, page.locator("#member-form-host"))
        form.wait_ready()
        form.kind("api")
        form.provider.select_option("openai")
        form.model.select_option(index=0)
        form.field("id").fill("openai-1")
        form.field("upstream").fill(upstream)
        form.field("credential_ref").fill(CRED_REF)
        form.submit.click()
        _wait_step(page, 2)
        with page.expect_response(lambda r: r.url.endswith("/api/deployments/openai-1/test")) as rr:
            page.locator("#test-run").click()
        assert rr.value.json()["ok"] is True
        expect(page.locator('#test-result[data-ok="true"]')).to_have_count(1)
        model = a.get("/api/deployments/openai-1")["model"]
        upstream_models = [r["model"] for r in httpx.get(f"{fake_base}/_fake/requests", trust_env=False).json()]
        assert model in upstream_models
        page.locator("#test-next").click()
        _wait_step(page, 3)
        expect(page.locator("#route-name")).to_have_value("chat")
        page.locator("#route-create").click()
        _wait_step(page, 4)
        example = page.locator("#curl-example").text_content() or ""
        ex_model = re.search(r'"model":\s*"([^"]+)"', example)
        assert ex_model and f"http://127.0.0.1:{a.port}/v1/chat/completions" in example
        with a.http() as c:
            r = c.post("/v1/chat/completions", json={"model": ex_model.group(1),
                                                      "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, _safe(r.text)
        elapsed = round(time.monotonic() - t0, 2)
        assert elapsed <= AC8_LIMIT_S
        screens.shot("wizard")
        AC8.write_text(json.dumps({"seconds": elapsed, "limit_seconds": AC8_LIMIT_S, "status": r.status_code,
                                   "note": "自動化回歸護欄;AC-8 以擁有者步驟 ④ 計時"}, ensure_ascii=False, indent=2)
                       + "\n", encoding="utf-8")
        obs["1_wizard"] = {"seconds": elapsed, "test_ok": True, "upstream_got_model": True,
                           "example_status": r.status_code}

        # ② UI-2:第二個 api 成員與 claude-cli 成員(假 CLI);連動選單與 single 停用(AC-20)
        open_members(page, a)
        adapters = a.get("/api/adapters")["modules"]
        form = _new_member(page)
        form.kind("api")
        api_providers = [p for m in adapters if m["available"] and "api" in m["kinds"] for p in m["providers"]]
        assert form.option_values(form.provider, enabled_only=True) == api_providers
        _check_disabled_providers(form, a)
        form.provider.select_option("openai")
        form.model.select_option(index=0)
        form.field("id").fill("api-b")
        form.field("upstream").fill(upstream)
        form.field("credential_ref").fill(CRED_REF)
        form.add_tag("coding")
        _submit_member(page, form)
        form = _new_member(page)
        form.kind("cli")
        cli_providers = [p for m in adapters if m["available"] and "cli" in m["kinds"] for p in m["providers"]]
        assert form.option_values(form.provider, enabled_only=True) == cli_providers
        _check_disabled_providers(form, a)
        form.provider.select_option("claude-cli")
        entries = yaml.safe_load(CLAUDE_MODELS.read_text(encoding="utf-8"))
        assert form.option_texts(form.model) == [e["label"] for e in entries] + [OTHER_TEXT]
        form.model.select_option(index=0)
        caps = entries[0]["capabilities"]
        assert form.caps() == {k: caps[k] for k in ("ctx", "tools", "vision", "efforts")}
        form.field("id").fill("claude-sub")
        form.add_tag("coding")
        _submit_member(page, form)
        form = _new_member(page)
        form.kind("cli")
        single = form.provider.locator('option[value="claude-cli"]')
        expect(single).to_be_disabled()
        expect(single).to_contain_text("已有 1 個,僅限 1 個")
        assert "claude-cli" in _check_disabled_providers(form, a)
        screens.shot("members")
        obs["2_members"] = {"api_providers": api_providers, "cli_providers": cli_providers,
                            "claude_models_match_yaml": True, "claude_cli_second_disabled": True}

        # ③ UI-3 群組 coding、UI-4 task route review(拖曳排序、儲存),送 3 次請求
        goto(page, a, "/ui/groups.html")
        page.locator("#new-group").click()
        page.locator("#group-name").fill("coding")
        box = page.locator('#group-editor input[data-tag-input="any"]')
        box.fill("coding")
        box.press("Enter")
        page.locator('#group-editor [data-role="save"]').click()
        expect(page.locator('#group-list button[data-group="coding"]')).to_contain_text("2 個成員")
        screens.shot("groups")
        goto(page, a, "/ui/routes.html")
        rform = page.locator("#route-new")
        expect(rform).to_have_attribute("data-state", "ready")
        rform.locator("#new-name").fill("review")
        rform.locator("#new-mode").select_option("task")
        rform.locator("#new-target-kind").select_option("group")
        rform.locator("#new-group").select_option("coding")
        rform.locator("#new-record").select_option(label="開")
        rform.locator("#new-submit").click()
        expect(page.locator('#routes-table tr[data-route="review"] [data-col="record"]')).to_have_text("開")
        screens.shot("routes")
        open_editor(page, a, "review")
        before = editor_slots(page)
        assert before == [["claude-sub"], ["api-b"]], before          # 預設:cli slot 1、api slot 2
        drag_slot(page, 1, 0)
        expect(page.locator("#dirty-banner")).to_be_visible()
        assert editor_slots(page) == [["api-b"], ["claude-sub"]]
        with page.expect_response(lambda r: r.url.endswith("/api/routes/review/effective-order")):
            page.locator("#btn-save").click()
        expect(page.locator("#dirty-banner")).to_be_hidden()
        assert route_order(a, "review") == [["api-b"], ["claude-sub"]]
        screens.shot("route-editor")
        served = []
        for i in range(3):
            r = _task(a, "review", i)
            assert r.status_code == 200, _safe(r.text)
            served.append(r.headers.get("X-Spillway-Served-By"))
        assert served == ["api-b"] * 3, served
        obs["3_route"] = {"default_slots": before, "after_drag": [["api-b"], ["claude-sub"]], "served_by": served}

        # ④ 額度(經 UI-2 編輯)、種入花費與冷卻,開啟 UI-1(AC-6)
        open_members(page, a, "api-b")
        form = MemberForm(page)
        form.wait_ready()
        form.set_budget("usd", "10", "monthly", "1")
        with page.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith("/api/deployments/api-b")):
            form.submit.click()
        expect(page.locator("#msgbar")).to_contain_text("已儲存成員")
        now = datetime.now(timezone.utc)
        insert_ledger(a, 9001, now - timedelta(seconds=1), deployment="api-b", route="review", cost_usd=4.0)
        until = (now + timedelta(hours=1)).replace(microsecond=0)
        set_cooldown(a, "openai-1", until, "rate_limited")
        goto(page, a, "/ui/")
        wait_loaded(page, "#members-table")
        dash = {d["id"]: d for d in a.get("/api/dashboard")["deployments"]}
        def row(dep_id: str) -> Any:
            return page.locator(f'#members-body tr[data-member="{dep_id}"]')

        expect(row("api-b").locator('[data-field="remaining"]')).to_have_text(f"${dash['api-b']['remaining']:.2f}")
        assert 5.0 < dash["api-b"]["remaining"] <= 6.0          # 10 − 4(種入)− ③ 的 3 次假上游花費
        expect(row("openai-1").locator('[data-field="cooling"]')).to_have_text(f"冷卻至 {_local(until)}(限流)")
        expect(row("claude-sub").locator('[data-field="remaining"]')).to_have_text("不限")
        screens.shot("overview")
        obs["4_overview"] = {d: ("cooling" if v["cooling"] else ("不限" if v["unlimited"] else
                                                                 f"${v['remaining']:.2f}"))
                             for d, v in dash.items()}

        # ⑤ UI-5:coding 的 5 種工具各取一次設定
        mcp_ok = []
        for tool in TOOLS:
            open_mcp(page, a, group="coding", tool=tool)
            assert mcp_variants(page) == _expected_variants(mcp_config(a, "coding", tool)), tool
            screens.html(f"mcp-{tool}")
            mcp_ok.append(tool)
        screens.shot("mcp")
        obs["5_mcp"] = {"group": "coding", "tools_matching_api": mcp_ok}

        # ⑥ UI-6:篩選、開啟一列內容、匯出 CSV
        goto(page, a, "/ui/ledger.html")
        wait_loaded(page, "#ledger-table")
        with page.expect_response(lambda r: "/api/ledger?" in r.url and "route=review" in r.url):
            page.locator("#f-route").select_option("review")
        wait_loaded(page, "#ledger-table")
        total = a.get("/api/ledger", params={"route": "review", "page_size": 1})["total"]
        expect(page.locator("#ledger-body tr[data-ledger-id]")).to_have_count(total)
        first_real = page.locator('#ledger-body tr[data-ledger-id]:not([data-ledger-id="9001"])').first
        with page.expect_response(lambda r: r.url.endswith("/payload")) as pr:
            first_real.click()
        assert pr.value.status == 200
        expect(page.locator('#payload-panel[data-loaded="true"]')).to_have_count(1)
        payload_text = page.locator("#payload-text").text_content() or ""
        assert "m4b-operation-" in payload_text
        pre_info = page.locator("#payload-text").evaluate("(el) => [el.tagName, el.childElementCount]")
        assert pre_info == ["PRE", 0], pre_info          # 純文字:<pre> 內沒有任何子元素
        screens.shot("ledger")
        page.locator("#payload-close").click()
        expect(page.locator("#payload-panel")).to_be_hidden()
        csv_bytes = _download(page, "#export-csv", tmp / "ledger.csv")
        csv_rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
        assert len(csv_rows) == total and {r["route_name"] for r in csv_rows} == {"review"}
        obs["6_ledger"] = {"filter": {"route": "review"}, "total": total, "csv_rows": len(csv_rows),
                           "payload_element": {"tag": pre_info[0], "child_elements": pre_info[1]}}

        # ⑦ UI-7:匯出 A(③ 之後)→ 第二資料目錄 replace 預覽 → 確認匯入 → 匯出 B(AC-10)
        open_settings(page, a)
        screens.shot("settings")
        file_a = _download(page, "#export-link", tmp / "A.yaml")
        with a.http() as c:
            assert file_a == c.get("/api/config/export").content
        b = ServeGw(tmp, "b", env["fake_bin"], env["serves"])
        assert all(x.startswith("127.0.0.1:") for x in procs.listening(b.port))
        open_settings(page, b)
        snap_b = db_snapshot(b)
        choose_import(page, file_a, mode="replace")
        assert import_requests(guard) == []
        page.locator("#import-preview").click()
        dialog = import_dialog(page)
        expect(dialog).to_be_visible()
        expect(dialog).to_have_class("dialog dialog-danger")
        expect(dialog.locator('[data-count="deployments.created"]')).to_have_text("3")
        previews = import_requests(guard)
        assert previews and all(parse_qs(urlsplit(u).query).get("dry_run") == ["true"] for u in previews), previews
        assert db_snapshot(b) == snap_b                  # 預覽(dry_run)不寫入
        with page.expect_response(lambda r: "/api/config/import" in r.url and "dry_run" not in r.url) as ir:
            dialog.locator('[data-role="confirm"]').click()
        result = ir.value.json()
        assert result["mode"] == "replace" and result["dry_run"] is False
        expect(page.locator("#import-result")).to_be_visible()
        file_b = _download(page, "#export-link", tmp / "B.yaml")
        assert file_a == file_b
        assert FAKE_KEY.encode() not in file_a
        obs["7_config"] = {"a_equals_b": True, "a_bytes": len(file_a), "import": {
            k: result[k] for k in ("created", "updated", "deleted")}, "backup": bool(result.get("backup"))}

        # ⑧ UI-8:四大類、可折疊、與 GET /api/mcp/catalog 一致、不含 /api/* 與 /v1(AC-22)
        open_reference(page, a)
        cat = a.get("/api/mcp/catalog")
        expect(page.locator("#ref-index details > summary")).to_have_text(["連線", "工具", "通知", "錯誤碼"])
        summary = page.locator('#ref-index details[data-category="tools"] > summary')
        links = page.locator('#ref-index details[data-category="tools"] a')
        summary.click()
        for link in links.all():
            expect(link).to_be_hidden()
        summary.click()
        for link in links.all():
            expect(link).to_be_visible()
        assert sorted(links.all_text_contents()) == sorted(t["name"] for t in cat["tools"])
        expect(page.locator('#ref-index details[data-category="notifications"] a')).to_have_text(
            [n["method"] for n in cat["notifications"]])
        items = [x.get_attribute("data-item") for x in page.locator("#ref-index a[data-item]").all()]
        for item in items:
            page.locator(f'#ref-index a[data-item="{item}"]').click()
            expect(page.locator("#ref-detail")).to_have_attribute("data-item", str(item))
            text = page.locator("body").inner_text()
            assert "/api/" not in text and "/v1" not in text, item
            if item == "errors-http":
                assert page.locator("#ref-detail tbody tr td:first-child").all_text_contents() == \
                    [e["code"] for e in cat["errors"]["http"]]
        screens.shot("api-reference")
        obs["8_api_reference"] = {"categories": ["連線", "工具", "通知", "錯誤碼"], "items": len(items),
                                  "tools": len(cat["tools"])}

        # ⑨ 子行程 serve + SPILLWAY_TEST_ADAPTER_PATHS:不可用模組停用並顯示原因(AC-21)
        mods = broken_adapter_dir(tmp / "mods")
        live = ServeGw(tmp, "c", env["fake_bin"], env["serves"], adapter_paths=[mods],
                       allowed_unavailable=(BROKEN_MODULE,))
        try:
            with live.http() as c:
                broken = next(m for m in c.get("/api/adapters").json()["modules"] if m["module"] == "broken")
            assert broken["available"] is False and broken["unavailable_reason"]
            open_members(page, live)
            form = _new_member(page)
            form.kind("api")
            opt = form.provider.locator(f'option[value="{BROKEN_PROVIDER}"]')
            expect(opt).to_be_disabled()
            expect(opt).to_contain_text(broken["unavailable_reason"])
            screens.shot("members-ac21")
        finally:
            page.goto("about:blank")       # 先離開該頁,再停 gateway(不留連線錯誤)
            live.stop()
        obs["9_ac21"] = {"started": True, "option_disabled": True}

        # ⑩ UI-7 重新產生 token:只在對話框出現,關閉後 DOM 不含
        open_settings(page, a)
        page.locator("#token-rotate").click()
        confirm = confirm_dialog(page)
        expect(confirm.locator(".dialog-title")).to_have_text("重新產生 token")
        with page.expect_response(lambda r: r.url.endswith(ROTATE)) as tr:
            confirm.locator('[data-role="confirm"]').click()
        new_token = tr.value.json()["token"]
        TOKENS.append(new_token)
        dlg = page.locator("#token-dialog")
        expect(dlg.locator("#token-value")).to_have_value(new_token)
        dlg.locator('[data-role="copy"]').click()
        dlg.locator('[data-role="close"]').click()
        expect(dlg).to_have_count(0)
        assert page_leaks(page, new_token) == []
        page.reload()
        wait_loaded(page, "#overview")
        expect(page.locator("#token-status")).to_have_text("已產生")
        screens.html("settings-after-rotate")
        assert screens.rotations == 1
        obs["10_token"] = {"shown_once_in_dialog": True, "dom_clean_after_close": True}

        # 全部 8 個畫面的 HTML(再巡一次,補齊未截圖的狀態)
        for label, href in NAV:
            goto(page, a, href)
            page.wait_for_load_state("networkidle")
            screens.html(f"nav-{label}")
    finally:
        screens.close()
        violations = [str(v) for v in close_and_collect(opened)]

    assert violations == [], "畫面違規:\n" + "\n".join(_safe(v) for v in violations)

    # NFR-4:資料庫、匯出檔、CSV、第二資料目錄 backups/、serve 日誌、畫面 HTML 與 API 回應
    secrets = [FAKE_KEY, *TOKENS]
    files = [tmp / "A.yaml", tmp / "B.yaml", tmp / "ledger.csv", *(s.log_path for s in env["serves"])]
    # 原始 stderr 與 stdout(日誌檔是遮罩副本;stdout 含 uvicorn access log,施工審議 T3 F1)
    stderr_hits = [str(s.log_path.name) for s in env["serves"] for sec in secrets
                   if sec.encode() in s.stderr_bytes() + s.stdout_bytes()]
    assert stderr_hits == [], stderr_hits
    for home in (tmp / "a", tmp / "b", tmp / "c"):
        files += [p for p in home.glob("spillway.db*") if p.is_file()]
    backups = sorted((tmp / "b" / "backups").glob("*"))
    assert backups, "第二資料目錄沒有 backups/"
    files += backups
    hits = [str(p.relative_to(tmp)) for p in files for s in secrets if s.encode() in p.read_bytes()]
    hits += [k for k, v in screens.captured for s in secrets if s.encode() in v]
    assert hits == [], hits
    readable = {k.split("?")[0].removeprefix("api:") for k, _ in screens.captured if k.startswith("api:")}
    missing = sorted(u for u in set(screens.unreadable) if u not in readable)
    assert missing == [], f"這些 /api/ 回應從未讀到 body,無法搜尋:{missing}"
    n_html = sum(1 for k, _ in screens.captured if k.startswith("html:"))
    n_api = sum(1 for k, _ in screens.captured if k.startswith("api:"))
    obs["nfr4"] = {"secrets": len(secrets), "files_searched": len(files), "html_searched": n_html,
                   "api_responses_searched": n_api, "api_bodies_unavailable": sorted(set(screens.unreadable)),
                   "hits": 0}
    obs["screenshots"] = screens.shots
    obs["console_errors"] = 0
    obs["non_local_requests"] = 0
    for k, v in obs.items():
        record(k, v)
    assert len(screens.shots) >= 10

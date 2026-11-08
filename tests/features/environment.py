"""behave 環境(CHG-20261030-02 T15;決策 11;CHG-20261107-03 T4 改為黑箱)。

執行:`python -m behave tests/features`(於 repo 根目錄;`-m` 讓根目錄在 `sys.path` 上,`tests` 可直接 import)。
步驟只經子行程 spillway(`SPILLWAY_CMD`,預設 `[sys.executable, "-m", "spillway"]`)的 HTTP、測試控制介面與
唯讀資料庫操作它,本行程不 import `spillway`、`litellm`(黑箱邊界,CHG-20261107-03 決策 9):
`before_all` 裝上 `sys.meta_path` 的 import 阻擋器,`after_all` 斷言 `sys.modules` 沒有它們。

護欄(對應 `tests/conftest.py`):
- 每個情境一個暫存 `SPILLWAY_HOME`(`tempfile.mkdtemp`),不是暫存目錄就中止;情境結束刪除。
- 清除真實的 API key 環境變數;`SPILLWAY_TEST_KEY` 為假金鑰 `sk-cred-behave-7777`(與 AC-15 prompt 字串不同),
  也明確傳給 serve 子行程。
- 假上游 `FakeApi`(`tests.blackbox.fakeapi.shared_fake_api()`,行程內、只綁 127.0.0.1)整個 run 共用一個,
  每個情境前後 reset。
- 真實 CLI 護欄(CHG-20261101-03 T3;決策 19):`before_all` 把 PATH 中含真實 `claude`、`codex` 的目錄移除並
  斷言;每個情境開始與結束都再斷言一次(情境中把真實執行檔放回 PATH 會讓該情境失敗),結束後還原成情境開始
  時已清理過的 PATH。serve 子行程的 PATH 另經 `safe_subprocess_env` 清理與斷言。
- 假 CLI:`before_all` 在暫存目錄產生假 `claude`、`codex`(`context.fake_cli_bin`,預設不在 serve 的 PATH 上);
  每個情境的 `context.fake_cli` 為 `FakeCli(SPILLWAY_HOME, context.fake_cli_bin)`,步驟以
  `context.gw.set_fake_bin(...)` 把它放到 serve 的 PATH 最前面。

驅動方式見 `tests/features/support/gateway.py`(每個情境一個 `tests.blackbox.harness.Serve`)。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from tests.blackbox.fakeapi import shared_fake_api
from tests.blackbox.guard import assert_no_spillway_modules, install_blocker, uninstall_blocker
from tests.blackbox.harness import REAL_KEY_VARS
from tests.fakes.cli_upstream import FakeCli, assert_only_fakes, install, sanitize_path
from tests.features.support.gateway import TEST_KEY, TEST_KEY_ENV, Gateway


def before_all(context):
    install_blocker()
    for var in REAL_KEY_VARS:
        os.environ.pop(var, None)
    os.environ["PATH"] = sanitize_path(os.environ.get("PATH", ""))
    assert_only_fakes()
    context.fake_cli_root = Path(tempfile.mkdtemp(prefix="spillway-fake-cli-")).resolve()
    context.fake_cli_bin = install(context.fake_cli_root / "bin")
    context.fake = shared_fake_api()      # 行程結束時(atexit)停止
    os.environ["SPILLWAY_LOCAL_API_BASE"] = context.fake.base_url + "/v1"


def after_all(context):
    root = getattr(context, "fake_cli_root", None)
    if root is not None:
        shutil.rmtree(root, ignore_errors=True)
    try:
        assert_no_spillway_modules("behave after_all")
    finally:
        uninstall_blocker()


# CHG-20261103-01 T9(決策 22,AM-5):Playwright(sync API)只在 m4b_ui feature 啟動與停止。sync API 會把主
# thread 標成「有執行中的事件迴圈」,所以只在這個 feature 期間存在;步驟不在主 thread 使用 asyncio。
M4B_UI_FEATURE = "m4b_ui.feature"


def _is_m4b_ui(feature) -> bool:
    return Path(str(feature.filename)).name == M4B_UI_FEATURE


def before_feature(context, feature):
    if not _is_m4b_ui(feature):
        return
    # 延遲 import:只有執行 m4b_ui 時才載入 Playwright(其他 feature 不需要它)
    from playwright.sync_api import sync_playwright

    context.playwright = sync_playwright().start()
    try:
        context.browser = context.playwright.chromium.launch(headless=True)
    except BaseException:
        context.playwright.stop()
        context.playwright = None
        raise


def after_feature(context, feature):
    if not _is_m4b_ui(feature):
        return
    import asyncio  # 延遲 import:只在 m4b_ui 結束時斷言用


    browser = getattr(context, "browser", None)
    pw = getattr(context, "playwright", None)
    try:
        if browser is not None:
            browser.close()
    finally:
        context.browser = None
        if pw is not None:
            pw.stop()
        context.playwright = None
    assert asyncio._get_running_loop() is None, "Playwright 的事件迴圈標記外洩到主 thread"


def before_scenario(context, scenario):
    tmp_root = Path(tempfile.gettempdir()).resolve()
    context.tmp = Path(tempfile.mkdtemp(prefix="spillway-behave-")).resolve()
    if not context.tmp.is_relative_to(tmp_root):
        raise RuntimeError("SPILLWAY_HOME 沒有指向暫存目錄,拒絕執行")
    home = context.tmp / "spillway-home"
    home.mkdir()
    assert_only_fakes()
    context.saved_env = {k: os.environ.get(k) for k in ("SPILLWAY_HOME", TEST_KEY_ENV, "PATH")}
    os.environ["SPILLWAY_HOME"] = str(home)
    os.environ[TEST_KEY_ENV] = TEST_KEY
    for var in REAL_KEY_VARS:
        os.environ.pop(var, None)
    context.fake.reset()
    context.gw = Gateway(home, context.fake)
    context.fake_cli = FakeCli(home, context.fake_cli_bin)
    # CHG-20261102-01 T8:步驟登記的收尾(另啟的 serve、MCP session、事件迴圈),在停止 gw 與刪除暫存目錄
    # 之前以登記的相反順序執行(behave 的 context.add_cleanup 在 after_scenario 之後才跑)
    context.scenario_teardown = []


def _run_scenario_teardown(context):
    errors = []
    for fn in reversed(getattr(context, "scenario_teardown", None) or []):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — 全部執行後再回報第一個
            errors.append(exc)
    context.scenario_teardown = []
    if errors:
        raise errors[0]


def _failed(scenario) -> bool:
    status = getattr(scenario, "status", None)
    return getattr(status, "name", str(status)) in ("failed", "error")


def after_scenario(context, scenario):
    gw = getattr(context, "gw", None)
    try:
        try:
            _run_scenario_teardown(context)
        finally:
            if gw is not None:
                # 通過時 Serve.stop() 斷言結束碼、stderr 無 Traceback/ERROR、未逾時;失敗時直接 kill
                gw.close(failed=_failed(scenario))
        assert_only_fakes()
    finally:
        context.fake.reset()
        for k, v in context.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(context.tmp, ignore_errors=True)

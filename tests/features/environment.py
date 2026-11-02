"""behave 環境(CHG-20261030-02 T15;決策 11)。

執行:`PYTHONPATH=src python -m behave tests/features`(於 repo 根目錄)。

護欄(對應 `tests/conftest.py`):
- import litellm 之前設定 `LITELLM_LOCAL_MODEL_COST_MAP=True`(不連網抓價格表)。
- 每個情境一個暫存 `SPILLWAY_HOME`(`tempfile.mkdtemp`),不是暫存目錄就中止;情境結束刪除。
- 清除真實的 API key 環境變數;`SPILLWAY_TEST_KEY` 為假金鑰 `sk-cred-behave-7777`(與 AC-15 prompt 字串不同)。
- 假上游 `FakeApiServer` 只綁 127.0.0.1,整個 run 共用一個,每個情境前後 reset。
- 真實 CLI 護欄(CHG-20261101-03 T3;決策 19):`before_all` 把 PATH 中含真實 `claude`、`codex` 的目錄移除並
  斷言;每個情境開始與結束都再斷言一次(情境中把真實執行檔放回 PATH 會讓該情境失敗),結束後還原成情境開始
  時已清理過的 PATH。
- 假 CLI:`before_all` 在暫存目錄產生假 `claude`、`codex`(`context.fake_cli_bin`,預設不在 PATH 上);
  每個情境的 `context.fake_cli` 為 `FakeCli(SPILLWAY_HOME, context.fake_cli_bin)`,步驟自行把
  `context.fake_cli_bin` 放到 PATH 最前面。

驅動方式見 `tests/features/support/gateway.py`(與 `open_spillway` 同一方式:create_app + lifespan +
httpx ASGITransport;FakeClock;固定 seed 的 SystemRng)。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT / "src"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from tests.fakes.api_upstream.server import FakeApiServer  # noqa: E402
from tests.fakes.cli_upstream import FakeCli, assert_only_fakes, install, sanitize_path  # noqa: E402
from tests.features.support.gateway import TEST_KEY, TEST_KEY_ENV, Gateway  # noqa: E402

REAL_KEY_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def before_all(context):
    for var in REAL_KEY_VARS:
        os.environ.pop(var, None)
    os.environ["PATH"] = sanitize_path(os.environ.get("PATH", ""))
    assert_only_fakes()
    context.fake_cli_root = Path(tempfile.mkdtemp(prefix="spillway-fake-cli-")).resolve()
    context.fake_cli_bin = install(context.fake_cli_root / "bin")
    context.fake = FakeApiServer().start()
    os.environ["SPILLWAY_LOCAL_API_BASE"] = context.fake.base_url + "/v1"


def after_all(context):
    fake = getattr(context, "fake", None)
    if fake is not None:
        fake.stop()
    root = getattr(context, "fake_cli_root", None)
    if root is not None:
        shutil.rmtree(root, ignore_errors=True)


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
    # CHG-20261102-01 T8:步驟登記的收尾(子行程 serve、live_gateway、MCP session),在關閉 gw 與刪除暫存目錄
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


def after_scenario(context, scenario):
    gw = getattr(context, "gw", None)
    try:
        try:
            _run_scenario_teardown(context)
        finally:
            if gw is not None:
                gw.close()
        assert_only_fakes()
    finally:
        context.fake.reset()
        for k, v in context.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(context.tmp, ignore_errors=True)

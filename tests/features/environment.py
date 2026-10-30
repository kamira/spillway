"""behave 環境(CHG-20261030-02 T15;決策 11)。

執行:`PYTHONPATH=src python -m behave tests/features`(於 repo 根目錄)。

護欄(對應 `tests/conftest.py`):
- import litellm 之前設定 `LITELLM_LOCAL_MODEL_COST_MAP=True`(不連網抓價格表)。
- 每個情境一個暫存 `SPILLWAY_HOME`(`tempfile.mkdtemp`),不是暫存目錄就中止;情境結束刪除。
- 清除真實的 API key 環境變數;`SPILLWAY_TEST_KEY` 為假金鑰 `sk-test-0000`。
- 假上游 `FakeApiServer` 只綁 127.0.0.1,整個 run 共用一個,每個情境前後 reset。

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
from tests.features.support.gateway import TEST_KEY, TEST_KEY_ENV, Gateway  # noqa: E402

REAL_KEY_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def before_all(context):
    for var in REAL_KEY_VARS:
        os.environ.pop(var, None)
    context.fake = FakeApiServer().start()
    os.environ["SPILLWAY_LOCAL_API_BASE"] = context.fake.base_url + "/v1"


def after_all(context):
    fake = getattr(context, "fake", None)
    if fake is not None:
        fake.stop()


def before_scenario(context, scenario):
    tmp_root = Path(tempfile.gettempdir()).resolve()
    context.tmp = Path(tempfile.mkdtemp(prefix="spillway-behave-")).resolve()
    if not context.tmp.is_relative_to(tmp_root):
        raise RuntimeError("SPILLWAY_HOME 沒有指向暫存目錄,拒絕執行")
    home = context.tmp / "spillway-home"
    home.mkdir()
    context.saved_env = {k: os.environ.get(k) for k in ("SPILLWAY_HOME", TEST_KEY_ENV)}
    os.environ["SPILLWAY_HOME"] = str(home)
    os.environ[TEST_KEY_ENV] = TEST_KEY
    for var in REAL_KEY_VARS:
        os.environ.pop(var, None)
    context.fake.reset()
    context.gw = Gateway(home, context.fake)


def after_scenario(context, scenario):
    gw = getattr(context, "gw", None)
    try:
        if gw is not None:
            gw.close()
    finally:
        context.fake.reset()
        for k, v in context.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(context.tmp, ignore_errors=True)

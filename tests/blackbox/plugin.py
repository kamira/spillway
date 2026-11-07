"""黑箱測試的 pytest fixture(CHG-20261107-03 T2;決策 8、9 ④)。由根 `tests/conftest.py` 的
`pytest_plugins` 註冊,任何目錄的測試都可使用;本模組不 import spillway。

- `serve_factory`(函式範圍):`make(home=None, **opts) -> Serve`,啟動並等到就緒(`opts` 見
  `tests.blackbox.harness.Serve`;`home` 省略時為 `tmp_path / "home-<n>"`)。測試結束時以相反順序
  `stop()`(含結束碼、Traceback、ERROR、逾時的斷言);測試本身失敗時改為 `kill()`。已由測試自行停止的
  serve 不再重複停止。
- `blackbox_guard`(session 範圍、autouse;施工審議 T2 F1):轉換後的目錄不必另外啟用。session 結束時,若本次
  收集到的測試**全部**位於 `guard.converted_roots()` 之內(「純執行」)或設了 `SPILLWAY_BLACKBOX_GUARD=1`,
  斷言 `sys.modules` 沒有 `spillway*`、`litellm*`;與 unit、integration 一起跑的 session 不檢查(它們本來就
  import spillway)。
- import 阻擋器(F2):純執行(收集完成時判定)或 `SPILLWAY_BLACKBOX_GUARD=1`(`pytest_configure` 時,連收集
  階段也擋)時,在 `sys.meta_path` 最前面放 `guard.SpillwayBlocker`,`spillway`/`spillway.*` 的 import 當場
  ImportError(訊息含「黑箱邊界」)。litellm 不擋(`prices.py` 以 `find_spec` 找它的位置),只在 session 結束時檢查。
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.blackbox.guard import (
    FORCE_ENV, REPO_ROOT, assert_no_spillway_modules, converted_roots, install_blocker, uninstall_blocker,
)
from tests.blackbox.harness import Serve

_PURE_KEY = pytest.StashKey[bool]()
_FAILED_KEY = pytest.StashKey[bool]()


def _under(path: Path, roots: list[str]) -> bool:
    try:
        rel = path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return False
    return any(rel == r or rel.startswith(r + "/") for r in roots)


def _forced() -> bool:
    return os.environ.get(FORCE_ENV) == "1"


def pytest_configure(config: pytest.Config) -> None:
    if _forced():
        install_blocker()


def pytest_unconfigure(config: pytest.Config) -> None:
    uninstall_blocker()


def pytest_collection_modifyitems(session: pytest.Session, config: pytest.Config, items: list[pytest.Item]) -> None:
    roots = converted_roots()
    pure = bool(items) and all(_under(Path(str(i.path)), roots) for i in items)
    config.stash[_PURE_KEY] = pure
    if pure:
        install_blocker()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Iterator[None]:
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call" and rep.failed:
        item.stash[_FAILED_KEY] = True


@pytest.fixture(scope="session", autouse=True)
def blackbox_guard(request: pytest.FixtureRequest) -> Iterator[None]:
    yield
    config = request.config
    if _forced() or config.stash.get(_PURE_KEY, False):
        assert_no_spillway_modules("pytest session")


@pytest.fixture
def serve_factory(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Callable[..., Serve]]:
    started: list[Serve] = []

    def make(home: Path | str | None = None, **opts: Any) -> Serve:
        s = Serve(home if home is not None else tmp_path / f"home-{len(started)}", **opts)
        started.append(s)
        return s.start()

    yield make
    failed = request.node.stash.get(_FAILED_KEY, False)
    errors: list[BaseException] = []
    for s in reversed(started):
        try:
            if failed:
                s.kill()
            else:
                s.stop()
        except BaseException as exc:  # noqa: BLE001 — 全部停完再回報
            errors.append(exc)
    if errors:
        raise errors[0]

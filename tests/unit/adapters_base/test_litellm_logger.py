"""`load_litellm()` 的 logger 等級下限(CHG-20261101-02 T8 L1;決策 13;使用者故事 4)。

七個 logger(LiteLLM 與它呼叫的 SDK、HTTP 程式庫)在 DEBUG 或 NOTSET 時改為 INFO,其他等級不動;
`propagate` 與 handler 清單不變。每個測試前後還原這七個 logger 與 root 的等級。
"""
from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from spillway.adapters.base.litellm_call import load_litellm

NAMES = ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "openai", "anthropic", "httpx", "httpcore")


@pytest.fixture(autouse=True)
def _restore_levels() -> Iterator[None]:
    load_litellm()   # 先 import 一次,讓 LiteLLM 自己的 handler 設定完成後再記錄狀態
    root = logging.getLogger()
    saved = {n: logging.getLogger(n).level for n in NAMES}
    saved_root = root.level
    try:
        yield
    finally:
        for n, lvl in saved.items():
            logging.getLogger(n).setLevel(lvl)
        root.setLevel(saved_root)


def _snapshot() -> dict[str, tuple[bool, list[logging.Handler]]]:
    return {n: (logging.getLogger(n).propagate, list(logging.getLogger(n).handlers)) for n in NAMES}


@pytest.mark.parametrize("start", [logging.NOTSET, logging.DEBUG])
def test_debug_or_notset_raised_to_info_under_debug_root(start: int) -> None:
    logging.getLogger().setLevel(logging.DEBUG)
    for n in NAMES:
        logging.getLogger(n).setLevel(start)
    load_litellm()
    assert {n: logging.getLogger(n).level for n in NAMES} == {n: logging.INFO for n in NAMES}
    for n in NAMES:
        assert not logging.getLogger(n).isEnabledFor(logging.DEBUG), n


@pytest.mark.parametrize("preset", [logging.INFO, logging.WARNING, logging.ERROR])
def test_other_levels_left_alone(preset: int) -> None:
    logging.getLogger().setLevel(logging.DEBUG)
    for n in NAMES:
        logging.getLogger(n).setLevel(preset)
    load_litellm()
    assert {n: logging.getLogger(n).level for n in NAMES} == {n: preset for n in NAMES}


def test_propagate_and_handlers_untouched() -> None:
    logging.getLogger().setLevel(logging.DEBUG)
    for n in NAMES:
        logging.getLogger(n).setLevel(logging.NOTSET)
    before = _snapshot()
    load_litellm()
    after = _snapshot()
    assert after == before


def test_floor_list_is_exactly_the_seven_loggers() -> None:
    from spillway.adapters.base.litellm_call import LOG_FLOOR_LOGGERS
    assert LOG_FLOOR_LOGGERS == NAMES

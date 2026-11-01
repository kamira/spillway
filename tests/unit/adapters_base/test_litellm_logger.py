"""`load_litellm()` 的 logger 等級下限(CHG-20261101-02 T8 L1;決策 13;使用者故事 4)。

七個 logger(LiteLLM 與它呼叫的 SDK、HTTP 程式庫)的等級低於 INFO(NOTSET、DEBUG 與自訂 1–19)時
改為 INFO,INFO 以上不動;`propagate` 與 handler 清單不變。
"""
from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from spillway.adapters.base.litellm_call import load_litellm

NAMES = ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "openai", "anthropic", "httpx", "httpcore")


@pytest.fixture(autouse=True)
def _restore_levels() -> Iterator[None]:
    """測試結束後把七個 logger 與 root 設回「測試開始時先呼叫一次 `load_litellm()` 之後」的等級
    (不是進入本測試前的原始等級:那次呼叫本身可能已套用下限)。"""
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


@pytest.mark.parametrize("start", [logging.NOTSET, 5, logging.DEBUG, 15])
def test_below_info_raised_to_info_under_debug_root(start: int) -> None:
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

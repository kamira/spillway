"""classify() 與 signals 規則格式(spec 5.3.1;T4 L1)。"""
from __future__ import annotations

import pytest

from spillway.adapters.base import (
    API_DEFAULT_SIGNALS,
    SignalsError,
    classify,
    resolve_signals,
    validate_signals,
)

# spec 5.3.1 表格的 claude-cli、codex-cli 列以同一格式表達(M3 才使用)
CLAUDE_CLI = {
    "quota": [{"equals": {"is_error": True}, "match": {"message": "credit|usage limit|extra usage"}}],
    "rate_limit": [{"match": {"message": "rate limit|overloaded"}}],
}
CODEX_CLI = {
    "quota": [{"exit_code_not": 0, "match": {"stderr": "usage limit|quota"}}],
    "rate_limit": [{"match": {"stderr": "rate limit|429"}}],
}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"http_status": 402, "message": "payment required"}, "quota_exhausted"),
        ({"http_status": 429, "error_code": "insufficient_quota"}, "quota_exhausted"),
        ({"http_status": 400, "error_code": "insufficient_quota"}, "quota_exhausted"),
        ({"http_status": 429, "message": "slow down"}, "rate_limited"),
        ({"http_status": 500, "message": "boom"}, "error"),
        ({"http_status": None, "message": "connection refused"}, "error"),
        ({}, "error"),
    ],
)
def test_api_row(raw, expected):
    assert classify(raw, API_DEFAULT_SIGNALS) == expected


def test_api_defaults_literal():
    assert API_DEFAULT_SIGNALS == {
        "quota": [{"http_status": 402}, {"error_code": "insufficient_quota"}],
        "rate_limit": [{"http_status": 429}],
    }


def test_http_status_as_numeric_string():
    assert classify({"http_status": "402"}, API_DEFAULT_SIGNALS) == "quota_exhausted"


def test_error_code_case_insensitive():
    assert classify({"error_code": "INSUFFICIENT_QUOTA"}, API_DEFAULT_SIGNALS) == "quota_exhausted"


def test_override_takes_precedence():
    params = {"signals": {"rate_limit": [{"http_status": 500}], "quota": [{"http_status": 429}]}}
    signals = resolve_signals(API_DEFAULT_SIGNALS, params)
    assert classify({"http_status": 500}, signals) == "rate_limited"
    assert classify({"http_status": 429}, signals) == "quota_exhausted"
    assert classify({"http_status": 402}, signals) == "error"


def test_override_per_key_keeps_other_default():
    signals = resolve_signals(API_DEFAULT_SIGNALS, {"signals": {"rate_limit": [{"http_status": 503}]}})
    assert classify({"http_status": 402}, signals) == "quota_exhausted"
    assert classify({"http_status": 503}, signals) == "rate_limited"
    assert classify({"http_status": 429}, signals) == "error"


def test_no_override_returns_defaults():
    assert resolve_signals(API_DEFAULT_SIGNALS, {}) == API_DEFAULT_SIGNALS
    assert resolve_signals(API_DEFAULT_SIGNALS, None) == API_DEFAULT_SIGNALS


def test_quota_checked_before_rate_limit():
    raw = {"http_status": 429, "error_code": "insufficient_quota"}
    assert classify(raw, API_DEFAULT_SIGNALS) == "quota_exhausted"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"is_error": True, "message": "You've hit your usage limit"}, "quota_exhausted"),
        ({"is_error": True, "message": "Credit balance is too low"}, "quota_exhausted"),
        ({"is_error": False, "message": "credit"}, "error"),
        ({"is_error": True, "message": "API Error: Overloaded"}, "rate_limited"),
        ({"is_error": True, "message": "Rate limit reached"}, "rate_limited"),
        ({"is_error": True, "message": "something else"}, "error"),
    ],
)
def test_claude_cli_row(raw, expected):
    validate_signals(CLAUDE_CLI)
    assert classify(raw, CLAUDE_CLI) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"exit_code": 1, "stderr": "error: usage limit reached"}, "quota_exhausted"),
        ({"exit_code": 2, "stderr": "Quota exceeded"}, "quota_exhausted"),
        ({"exit_code": 0, "stderr": "quota"}, "error"),
        ({"exit_code": None, "stderr": "quota"}, "error"),
        ({"exit_code": 1, "stderr": "HTTP 429 Too Many Requests"}, "rate_limited"),
        ({"exit_code": 1, "stderr": "crash"}, "error"),
    ],
)
def test_codex_cli_row(raw, expected):
    validate_signals(CODEX_CLI)
    assert classify(raw, CODEX_CLI) == expected


def test_exit_code_and_list_values():
    s = {"quota": [{"exit_code": [3, 4]}], "rate_limit": [{"http_status": [429, 503]}]}
    assert classify({"exit_code": 4}, s) == "quota_exhausted"
    assert classify({"http_status": 503}, s) == "rate_limited"
    assert classify({"exit_code": 5}, s) == "error"


def test_match_missing_field_is_no_match():
    s = {"quota": [{"match": {"stderr": "quota"}}]}
    assert classify({"message": "quota"}, s) == "error"


def test_empty_signals_always_error():
    assert classify({"http_status": 402}, {}) == "error"


@pytest.mark.parametrize(
    "bad",
    [
        [],                                         # 不是 dict
        {"quota": {"http_status": 402}},            # 規則清單不是 list
        {"quota": [{}]},                            # 空規則
        {"quota": [{"bogus": 1}]},                  # 未知條件
        {"quota": [{"http_status": "abc"}]},
        {"quota": [{"http_status": True}]},
        {"quota": [{"error_code": 5}]},
        {"quota": [{"match": {"message": "("}}]},   # 壞的正規式
        {"quota": [{"match": "credit"}]},
        {"quota": [{"equals": []}]},
        {"limits": []},                             # 未知的 signals 鍵
    ],
)
def test_invalid_signals_raise(bad):
    with pytest.raises(SignalsError):
        validate_signals(bad)
    with pytest.raises(SignalsError):
        classify({"http_status": 402}, bad)


def test_override_must_be_mapping():
    with pytest.raises(SignalsError):
        resolve_signals(API_DEFAULT_SIGNALS, {"signals": "402"})


@pytest.mark.parametrize("status,expected", [
    ("429", "rate_limited"), (" 402 ", "quota_exhausted"), ("--5", "error"), ("²", "error"),
    ("-", "error"), ("4 29", "error"), ("١٢٣", "error"),
])
def test_string_http_status_parsed_strictly_never_raises(status, expected):
    """S-T4-2:字串 status 只收 ASCII 整數;`isdigit()` 為 True 但 int() 失敗的值不外拋。"""
    assert classify({"http_status": status}, API_DEFAULT_SIGNALS) == expected

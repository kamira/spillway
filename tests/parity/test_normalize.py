"""正規化與相等定義自測(CHG-20261108-01 T3 L1;spec 15.5、15.7、15.8、15.9、15.10;契約清單 spec 15)。"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from tests.blackbox.mask import MASK
from tests.parity.corpus import CORPUS_KEYS, SAMPLE_TOKEN, SEEDED_PARAMS_KEY, sample_capture
from tests.parity.normalize import (
    DB_ROWS, ERRORS, JSON_BODY, MODE_MALFORMED, UPSTREAM, Capture, Exchange, ParsedJson, Report, compare_captures, diff_values,
    mask_capture, normalize_capture, parse_strict, presence_of, route_of, scan_capture,
)

KEY = CORPUS_KEYS[0]


def diffs(a, b):
    return list(diff_values(a, b))


# ---------------------------------------------------------------- 15.5


@pytest.mark.parametrize("a,b", [
    (1, True), (0, False), (1, "1"), (None, 0), ("", None), ([], {}), (True, "true"), ([1], {"0": 1}),
])
def test_values_of_different_types_are_not_equal(a, b):
    assert diffs(a, b) == [("$", "type")]


def test_bool_is_excluded_before_numeric_comparison():
    assert diffs(True, 1.0) == [("$", "type")]
    assert diffs({"a": False}, {"a": 0}) == [("$.a", "type")]


def test_integer_equals_float():
    assert diffs(1, 1.0) == [] and diffs({"a": [2]}, {"a": [2.0]}) == []


def test_missing_key_is_not_null():
    assert diffs({"a": None}, {}) == [("$.a", "missing_key")]


def test_object_key_order_does_not_matter():
    assert diffs({"a": 1, "b": 2}, {"b": 2, "a": 1}) == []


def test_array_order_matters():
    assert diffs([1, 2], [2, 1]) == [("$[0]", "value"), ("$[1]", "value")]


def test_array_length_difference():
    assert diffs([1], [1, 2]) == [("$[1]", "missing_item")]


def test_diff_reports_nested_paths():
    assert diffs({"a": {"b": [{"c": 1}]}}, {"a": {"b": [{"c": 2}]}}) == [("$.a.b[0].c", "value")]


# ---------------------------------------------------------------- 嚴格 JSON


@pytest.mark.parametrize("text", ['{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}', "NaN", "{bad", '{"a": 1e999}', "-1e999"])
def test_strict_parser_rejects_nan_infinity_and_garbage(text):
    with pytest.raises(ValueError):
        parse_strict(text)


def test_strict_parser_parses_valid_json():
    assert parse_strict(b'{"a": 1.5}') == {"a": 1.5}


def test_response_with_nan_is_a_shape_violation():
    a, b = sample_capture("a"), sample_capture("b")
    b.exchanges[0] = replace(b.exchanges[0], body=b'{"id": NaN}')
    report = compare_captures(a, b)
    assert any(d.path == "$" and d.kind == "shape" and d.side == "b" for d in report.differences), report.text()


# ---------------------------------------------------------------- 路徑


@pytest.mark.parametrize("method,path,surface", [
    ("POST", "/v1/chat/completions", "POST /v1/chat/completions"),
    ("GET", "/api/settings", "GET /api/settings"),
    ("GET", "/api/settings?x=1", "GET /api/settings"),
    ("GET", "/api/settings/context", None),
    ("GET", "/api/deployments/alpha", "GET /api/deployments/{id}"),
    ("PUT", "/api/deployments/alpha", "PUT /api/deployments/{id}"),
    ("POST", "/api/deployments/alpha/test", "POST /api/deployments/{id}/test"),
    ("GET", "/api/ledger/3/payload", "GET /api/ledger/{id}/payload"),
])
def test_route_of(method, path, surface):
    assert route_of(method, path) == surface


def test_identical_sample_is_equal_after_normalization():
    report = compare_captures(sample_capture("a"), sample_capture("b"))
    assert report.ok, report.text()


def _body(capture: Capture, i: int) -> dict:
    return json.loads(capture.exchanges[i].body)


def _with_body(capture: Capture, i: int, body: dict) -> None:
    capture.exchanges[i] = replace(capture.exchanges[i], body=json.dumps(body).encode())


def test_shape_mismatch_is_not_replaced():
    """15.9.2:形狀不符不替換(值留在原處),報形狀差異。"""
    a = sample_capture("a")
    body = _body(a, 0)
    body["id"] = "not-a-chat-id"
    _with_body(a, 0, body)
    view, shapes = normalize_capture(mask_capture(a), "a")
    assert view.responses[0].body["id"] == "not-a-chat-id"
    assert [(d.path, d.kind) for d in shapes] == [("$.id", "shape")]


def test_replacement_numbers_first_appearance_within_scenario():
    """15.9.3:同值同號、首次出現順序編號。"""
    a = sample_capture("a")
    first = _body(a, 0)
    a.exchanges.append(Exchange("POST", "/v1/chat/completions", 200, (("content-type", "application/json"),),
                                json.dumps({**first, "id": "chatcmpl-" + "c" * 32}).encode()))
    a.exchanges.append(Exchange("POST", "/v1/chat/completions", 200, (("content-type", "application/json"),),
                                json.dumps(first).encode()))
    view, _ = normalize_capture(mask_capture(a), "a")
    ids = [r.body["id"] for r in view.responses if r.status == 200 and r.body and "id" in r.body]
    assert ids == ["<id-1>", "<id-2>", "<id-1>"]


def test_different_id_patterns_across_sides_are_equal():
    a, b = sample_capture("a"), sample_capture("b")
    assert json.loads(a.exchanges[0].body)["id"] != json.loads(b.exchanges[0].body)["id"]
    assert compare_captures(a, b).ok


def test_int_field_requires_integer_type():
    """15.5.3 / 15.9.5:必須為整數的欄位另比型別(1.0 不是整數)。"""
    a, b = sample_capture("a"), sample_capture("b")
    body = _body(b, 0)
    body["created"] = float(body["created"])
    _with_body(b, 0, body)
    report = compare_captures(a, b)
    assert ("$.created", "shape", "b") in {(d.path, d.kind, d.side) for d in report.differences}, report.text()


def test_token_field_must_be_masked_after_mask_stage():
    a = sample_capture("a")
    _, shapes = normalize_capture(mask_capture(a), "a")
    assert not shapes
    # 明文 token 沒有在 sentinel 內時遮罩不掉,④ 報形狀不符
    a.tokens = ()
    _, shapes = normalize_capture(mask_capture(a), "a")
    assert [(d.path, d.kind) for d in shapes] == [("$.token", "shape")]


# ---------------------------------------------------------------- 錯誤


def _error_report(status: int, a_msg: str, b_msg: str, code: str = "validation_error", mode: str = "") -> Report:
    a, b = sample_capture("a"), sample_capture("b")
    for cap, msg in ((a, a_msg), (b, b_msg)):
        cap.exchanges[:] = [Exchange("POST", "/v1/chat/completions", status, (("content-type", "application/json"),),
                                     json.dumps({"error": {"code": code, "message": msg}}).encode(), mode)]
    return compare_captures(a, b)


def test_422_compares_only_field_path_sets():
    assert _error_report(422, "body.messages: A", "body.messages: B").ok


def test_422_field_set_difference_detected():
    report = _error_report(422, "body.messages: A", "body.model: A")
    assert any(d.surface == ERRORS and d.path.startswith("$.error.message") for d in report.differences), report.text()


def test_422_ignores_order_and_segments_without_field_path():
    assert _error_report(422, "body.a: x; body.b: y; 沒有路徑", "body.b: x; body.a: z").ok


def test_malformed_marker_compares_only_code_and_status():
    assert _error_report(422, "anything", "something else", mode=MODE_MALFORMED).ok


def test_no_candidate_is_verbatim():
    report = _error_report(503, "沒有;嘗試:a:error", "沒有;嘗試:b:error", code="no_candidate")
    assert [d.path for d in report.differences] == ["$.error.message"]


def test_other_message_only_needs_non_empty_string():
    assert _error_report(404, "找不到 a", "not found", code="not_found").ok


def test_empty_message_is_rejected():
    report = _error_report(404, "", "x", code="not_found")
    assert any(d.kind == "shape" and d.side == "a" for d in report.differences), report.text()


def test_error_code_always_compared():
    a, b = sample_capture("a"), sample_capture("b")
    b.exchanges[1] = replace(b.exchanges[1], body=json.dumps({"error": {"code": "other", "message": "x"}}).encode())
    report = compare_captures(a, b)
    assert any(d.surface == ERRORS and d.path == "$.error.code" for d in report.differences), report.text()


def test_non_json_response_only_compares_status_and_headers():
    a, b = sample_capture("a"), sample_capture("b")
    assert a.exchanges[7].body != b.exchanges[7].body
    assert compare_captures(a, b).ok


# ---------------------------------------------------------------- 契約 header 的例外(15.4)


def _single(status: int, code: str, cache: str) -> Capture:
    cap = sample_capture("a")
    cap.exchanges[:] = [Exchange("GET", "/api/x", status, (("content-type", "application/json"), ("cache-control", cache)),
                                 json.dumps({"error": {"code": code, "message": "m"}}).encode())]
    return cap


@pytest.mark.parametrize("status,code", [(500, "internal_error"), (503, "shutting_down"), (403, "host_not_allowed"),
                                         (403, "origin_not_allowed")])
def test_cache_headers_not_compared_on_excepted_responses(status, code):
    assert compare_captures(_single(status, code, "no-store"), _single(status, code, "public")).ok


@pytest.mark.parametrize("status,code", [(403, "recursion_blocked"), (404, "not_found"), (200, "x")])
def test_cache_headers_compared_on_other_responses(status, code):
    report = compare_captures(_single(status, code, "no-store"), _single(status, code, "public"))
    assert [d.path for d in report.differences] == ["cache-control"], report.text()


# ---------------------------------------------------------------- DB


def test_db_api_base_only_replaces_fake_prefix():
    a = sample_capture("a")
    view, _ = normalize_capture(mask_capture(a), "a")
    typeof, value = view.db["deployments"][0]["params"]
    assert typeof == "text" and "<fake-base-url>/v1" in value and "127.0.0.1" not in value


def test_db_literal_columns_compare_bytes():
    a, b = sample_capture("a"), sample_capture("b")
    b.db["deployments"][0]["capabilities"] = ("text", '{"ctx":1}')     # 語意相同、位元組不同
    report = compare_captures(a, b)
    assert [(d.surface, d.path) for d in report.differences] == [(DB_ROWS, "capabilities")]


def test_db_typeof_compared_separately():
    a, b = sample_capture("a"), sample_capture("b")
    b.db["usage_ledger"][0]["tokens_in"] = ("real", 11.0)
    report = compare_captures(a, b)
    assert [(d.path, d.kind) for d in report.differences] == [("tokens_in", "typeof")]


def test_db_request_json_parsed_when_not_truncated():
    a, b = sample_capture("a"), sample_capture("b")
    assert compare_captures(a, b).ok      # 鍵順序不同,解析後相等
    view, _ = normalize_capture(mask_capture(a), "a")
    assert isinstance(view.db["usage_payloads"][0]["request_json"][1], ParsedJson)


def test_db_request_json_verbatim_when_truncated():
    a, b = sample_capture("a"), sample_capture("b")
    for cap in (a, b):
        cap.db["usage_payloads"][0]["truncated"] = ("integer", 1)
    report = compare_captures(a, b)
    assert [(d.surface, d.path) for d in report.differences] == [(DB_ROWS, "request_json")]


def test_db_settings_hash_shape_checked_not_reported_by_value():
    a, b = sample_capture("a"), sample_capture("b")
    b.db["settings"][0]["value"] = ("text", "nothex")
    report = compare_captures(a, b)
    assert [(d.surface, d.kind, d.side) for d in report.differences] == [(DB_ROWS, "shape", "b")]
    assert "nothex" not in report.text()


def test_db_type_only_columns():
    a, b = sample_capture("a"), sample_capture("b")
    assert compare_captures(a, b).ok      # cost_usd、est_tokens_in 值不同
    b.db["usage_ledger"][0]["cost_usd"] = ("null", None)
    report = compare_captures(a, b)
    assert [(d.path, d.kind) for d in report.differences] == [("cost_usd", "typeof")]


# ---------------------------------------------------------------- 上游


def test_upstream_requires_listed_headers_on_both_sides():
    a, b = sample_capture("a"), sample_capture("b")
    for cap in (a, b):
        cap.upstream[0].header_names = ("content-type",)
    report = compare_captures(a, b)
    assert {d.path for d in report.differences if d.kind == "required_missing"} == {"header:authorization"}


def test_upstream_ignored_headers_are_not_compared():
    a, b = sample_capture("a"), sample_capture("b")
    b.upstream[0].header_names = (*b.upstream[0].header_names, "user-agent", "x-stainless-os", "accept-encoding")
    assert compare_captures(a, b).ok


def test_upstream_auth_header_ignored_without_credential():
    a, b = sample_capture("a"), sample_capture("b")
    assert "authorization" in b.upstream[2].header_names and "authorization" not in a.upstream[2].header_names
    assert compare_captures(a, b).ok


def test_upstream_unknown_header_on_one_side_differs():
    a, b = sample_capture("a"), sample_capture("b")
    b.upstream[0].header_names = (*b.upstream[0].header_names, "x-novel")
    report = compare_captures(a, b)
    assert [(d.surface, d.path) for d in report.differences] == [(UPSTREAM, "header:x-novel")]


def test_upstream_non_json_compares_length_only():
    a, b = sample_capture("a"), sample_capture("b")
    for cap in (a, b):
        cap.upstream[0].has_json, cap.upstream[0].json_body = False, None
    b.upstream[0].body_length = 999
    report = compare_captures(a, b)
    assert [(d.path, d.kind) for d in report.differences] == [("length", "value")]


# ---------------------------------------------------------------- 遮罩與處理順序


def test_processing_order_scan_happens_before_masking():
    """一側帶 sentinel、另一側已遮罩:遮罩後兩側相等,但 ② 必須已判不一致。"""
    a, b = sample_capture("a"), sample_capture("b")
    for cap, value in ((a, MASK), (b, KEY)):
        body = _body(cap, 0)
        body["choices"][0]["message"]["content"] = value
        _with_body(cap, 0, body)
    report = compare_captures(a, b)
    hits = [d for d in report.differences if d.kind == "sentinel"]
    assert [(d.surface, d.side) for d in hits] == [(JSON_BODY, "b")], report.text()
    # 比較的是遮罩後的值:拿掉 sentinel 後兩側相等
    va, _ = normalize_capture(mask_capture(a), "a")
    vb, _ = normalize_capture(mask_capture(b), "b")
    assert va.responses[0].body["choices"] == vb.responses[0].body["choices"]


def test_mask_keeps_json_valid_with_escapes():
    a = sample_capture("a")
    body = _body(a, 0)
    body["note"] = 'bearer abc"def'
    _with_body(a, 0, body)
    view = mask_capture(a)
    assert view.exchanges[0].kind == "json"


def test_masked_token_never_reaches_view():
    a = sample_capture("a")
    view = mask_capture(a)
    assert view.exchanges[9].body["token"] == MASK and SAMPLE_TOKEN not in json.dumps(view.exchanges[9].body)


def test_scan_exempts_rotated_token_and_prompt_forwarding():
    a = sample_capture("a")
    assert scan_capture(a, "a") == []      # rotate 的 $.token 不掃描;上游 body 不在掃描範圍
    a.upstream[0].json_body = {"messages": [{"role": "user", "content": KEY}]}
    assert scan_capture(a, "a") == []


# ---------------------------------------------------------------- 13.4.5 種入金鑰(15.10.3)與存在性記錄(15.10.5)


_JSON = (("content-type", "application/json"),)


def _seed_positions(capture: Capture, value: str) -> None:
    """把值放在 15.10.3 豁免的三個位置:DB、單一成員 API 回顯、清單 API 回顯。"""
    capture.db["deployments"][0]["params"] = ("text", json.dumps({"api_key": value}))
    capture.exchanges[:] = [
        Exchange("GET", "/api/deployments/alpha", 200, _JSON, json.dumps({"id": "alpha", "params": {"api_key": value}}).encode()),
        Exchange("GET", "/api/deployments", 200, _JSON,
                 json.dumps({"deployments": [{"id": "alpha", "params": {"api_key": value}}]}).encode()),
    ]


def _put_seeded_in_log(b: Capture) -> None:
    b.log_lines.append(f"SPILLWAY-LOG INFO mod {SEEDED_PARAMS_KEY}")


def _put_seeded_in_ledger_error(b: Capture) -> None:
    b.db["usage_ledger"][0]["error"] = ("text", SEEDED_PARAMS_KEY)


def _put_seeded_in_response(b: Capture) -> None:
    body = _body(b, 0)
    body["leak"] = SEEDED_PARAMS_KEY
    _with_body(b, 0, body)


@pytest.mark.parametrize("place", [_put_seeded_in_log, _put_seeded_in_ledger_error, _put_seeded_in_response],
                         ids=["log", "ledger_error", "response_field"])
def test_seeded_key_outside_exempt_positions_is_sentinel(place):
    a, b = sample_capture("a"), sample_capture("b")
    for cap in (a, b):
        cap.seeded_param_ids = frozenset({"alpha"})
    place(b)
    report = compare_captures(a, b)
    assert any(d.kind == "sentinel" and d.side == "b" for d in report.differences), report.text()
    assert SEEDED_PARAMS_KEY not in report.text()


def test_seeded_key_in_exempt_positions_not_flagged():
    a = sample_capture("a")
    a.seeded_param_ids = frozenset({"alpha"})
    _seed_positions(a, SEEDED_PARAMS_KEY)
    assert scan_capture(a, "a") == []


def test_seeded_exemption_is_limited_to_seeded_members():
    a = sample_capture("a")
    a.seeded_param_ids = frozenset({"other"})
    _seed_positions(a, SEEDED_PARAMS_KEY)
    assert {(d.surface, d.kind) for d in scan_capture(a, "a")} == {(DB_ROWS, "sentinel"), (JSON_BODY, "sentinel")}
    assert len(scan_capture(a, "a")) == 3


PROMPT_PATH = "$.messages[0].content"


def _prompt(capture: Capture, text: str) -> None:
    capture.upstream[0].json_body = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": text}]}


def test_upstream_prompt_masked_on_one_side_differs():
    a, b = sample_capture("a"), sample_capture("b")
    _prompt(a, f"my key is {KEY}")
    _prompt(b, f"my key is {MASK}")
    report = compare_captures(a, b)
    assert (UPSTREAM, PROMPT_PATH, "sentinel_presence") in {(d.surface, d.path, d.kind) for d in report.differences}, report.text()
    assert KEY not in report.text()


def test_upstream_prompt_same_on_both_sides_is_equal():
    a, b = sample_capture("a"), sample_capture("b")
    _prompt(a, f"my key is {KEY}")
    _prompt(b, f"my key is {KEY}")
    report = compare_captures(a, b)
    assert report.ok, report.text()


def _seeded_pair(value_a: str, value_b: str) -> tuple[Capture, Capture]:
    a, b = sample_capture("a"), sample_capture("b")
    for cap, value in ((a, value_a), (b, value_b)):
        cap.seeded_param_ids = frozenset({"alpha"})
        _seed_positions(cap, value)
    return a, b


def test_seeded_echo_masked_on_one_side_differs_in_db():
    a, b = _seeded_pair(SEEDED_PARAMS_KEY, SEEDED_PARAMS_KEY)
    b.db["deployments"][0]["params"] = ("text", json.dumps({"api_key": MASK}))
    report = compare_captures(a, b)
    assert [(d.surface, d.location, d.path, d.kind) for d in report.differences] == \
        [(DB_ROWS, "deployments[0]", "params", "sentinel_presence")], report.text()
    assert SEEDED_PARAMS_KEY not in report.text()


def test_seeded_echo_masked_on_one_side_differs_in_api():
    a, b = _seeded_pair(SEEDED_PARAMS_KEY, SEEDED_PARAMS_KEY)
    b.exchanges[0] = replace(b.exchanges[0], body=json.dumps({"id": "alpha", "params": {"api_key": MASK}}).encode())
    report = compare_captures(a, b)
    assert [(d.surface, d.path, d.kind) for d in report.differences] == [(JSON_BODY, "$.params.api_key", "sentinel_presence")], report.text()
    assert SEEDED_PARAMS_KEY not in report.text()


def test_rotated_token_is_not_in_the_presence_record():
    """15.10.5 末句:rotate 的 `$.token` 依 15.9.5,不適用存在性記錄。"""
    assert not any(path == "$.token" for _, _, path in presence_of(sample_capture("a")))


def test_seeded_echo_plain_on_both_sides_is_equal():
    a, b = _seeded_pair(SEEDED_PARAMS_KEY, SEEDED_PARAMS_KEY)
    report = compare_captures(a, b)
    assert report.ok, report.text()

"""突變矩陣(CHG-20261108-01 T3 L4;AM-10;spec 15):comparator 層每個已定義比對面與每條正規化規則至少一案,加端到端突變。"""
from __future__ import annotations

import json
import shlex
import sys
from dataclasses import replace
from typing import Any

import pytest

from tests.blackbox.mask import MASK
from tests.parity import test_normalize
from tests.parity.corpus import CORPUS_KEYS, SCENARIOS, sample_capture
from tests.parity.normalize import (
    CONTRACT_HEADERS, DB_COLUMN_RULES, DB_ROWS, DB_SURFACE, ERRORS, HEADERS, JSON_BODY, PATH_RULES, STATUS, UPSTREAM, Capture, Exchange,
    PathRule, Report, compare_captures, scan_capture,
)
from tests.parity.runner import CMD_B_ENV, mutant_cmd, run_pair

KEY = CORPUS_KEYS[0]
_JSON = (("content-type", "application/json"),)


def _pair() -> tuple[Capture, Capture]:
    return sample_capture("a"), sample_capture("b")


def _edit_body(cap: Capture, i: int, fn: Any) -> None:
    body = json.loads(cap.exchanges[i].body)
    fn(body)
    cap.exchanges[i] = replace(cap.exchanges[i], body=json.dumps(body).encode())


def _status(b: Capture) -> None:
    b.exchanges[0] = replace(b.exchanges[0], status=201)


def _set_header(b: Capture, name: str, value: str | None) -> None:
    headers = [(k, v) for k, v in b.exchanges[0].headers if k.lower() != name.lower()]
    if value is not None:
        headers.append((name, value))
    b.exchanges[0] = replace(b.exchanges[0], headers=tuple(headers))


def _content(b: Capture) -> None:
    _edit_body(b, 0, lambda d: d["choices"][0]["message"].__setitem__("content", "changed"))


def _type(b: Capture) -> None:
    _edit_body(b, 0, lambda d: d.__setitem__("object", 1))


def _missing_key(b: Capture) -> None:
    _edit_body(b, 0, lambda d: d.pop("object"))


def _array_order(b: Capture) -> None:
    _edit_body(b, 0, lambda d: d["choices"].reverse())


def _array_item_added(b: Capture) -> None:
    _edit_body(b, 0, lambda d: d["choices"].append({"index": 2}))


def _one_second(b: Capture) -> None:
    b.db["usage_ledger"][0]["ts"] = ("text", "2026-11-08T00:00:01Z")


def _error_code(b: Capture) -> None:
    _edit_body(b, 1, lambda d: d["error"].__setitem__("code", "other_code"))


def _error_message_verbatim(b: Capture) -> None:
    _edit_body(b, 2, lambda d: d["error"].__setitem__("message", "不同"))


def _unknown_path_message(b: Capture) -> None:
    _edit_body(b, 3, lambda d: d["error"].__setitem__("message", "Nope"))


def _db_value(b: Capture) -> None:
    b.db["usage_ledger"][0]["tokens_in"] = ("integer", 12)


def _db_row_missing(b: Capture) -> None:
    b.db["settings"].pop()


def _db_user_version(b: Capture) -> None:
    b.user_version = 3


def _upstream_path(b: Capture) -> None:
    b.upstream[0].path = "/v1/other"


def _upstream_count(b: Capture) -> None:
    b.upstream.pop()


def _upstream_body(b: Capture) -> None:
    b.upstream[0].json_body = {"model": "gpt-4o-mini", "messages": [], "extra": True}


def _upstream_method(b: Capture) -> None:
    b.upstream[3].method = "HEAD"


def _upstream_query(b: Capture) -> None:
    b.upstream[3].query_names = ("q",)


def _upstream_required_header(b: Capture) -> None:
    b.upstream[1].header_names = ("anthropic-version", "content-type")


# (case 名稱, 突變, 預期比對面, 預期路徑子字串)
SURFACE_CASES: dict[str, list[tuple[str, Any, str, str]]] = {
    STATUS: [("status_changed", _status, STATUS, "status")],
    HEADERS: [
        ("served_by_changed", lambda b: _set_header(b, "X-Spillway-Served-By", "beta"), HEADERS, "x-spillway-served-by"),
        ("slot_missing", lambda b: _set_header(b, "X-Spillway-Slot", None), HEADERS, "x-spillway-slot"),
        ("est_cost_bad_shape", lambda b: _set_header(b, "X-Spillway-Est-Cost", "1.5"), HEADERS, "x-spillway-est-cost"),
        ("cache_control_changed", lambda b: _set_header(b, "cache-control", "public"), HEADERS, "cache-control"),
        ("nosniff_missing", lambda b: _set_header(b, "x-content-type-options", None), HEADERS, "x-content-type-options"),
    ],
    JSON_BODY: [
        ("content_changed", _content, JSON_BODY, "$.choices[0].message.content"),
        ("type_changed", _type, JSON_BODY, "$.object"),
        ("key_missing", _missing_key, JSON_BODY, "$.object"),
        ("array_order_changed", _array_order, JSON_BODY, "$.choices[0]"),
        ("array_item_added", _array_item_added, JSON_BODY, "$.choices[2]"),
    ],
    ERRORS: [
        ("error_code_changed", _error_code, ERRORS, "$.error.code"),
        ("no_candidate_message_changed", _error_message_verbatim, ERRORS, "$.error.message"),
        ("unknown_path_message_changed", _unknown_path_message, ERRORS, "$.error.message"),
    ],
    DB_ROWS: [
        ("one_second_time_difference", _one_second, DB_ROWS, "ts"),
        ("cell_changed", _db_value, DB_ROWS, "tokens_in"),
        ("row_missing", _db_row_missing, DB_ROWS, "row_count"),
        ("user_version", _db_user_version, DB_ROWS, "user_version"),
    ],
    UPSTREAM: [
        ("path_changed", _upstream_path, UPSTREAM, "path"),
        ("count_changed", _upstream_count, UPSTREAM, "count"),
        ("body_changed", _upstream_body, UPSTREAM, "$.extra"),
        ("method_changed", _upstream_method, UPSTREAM, "method"),
        ("query_names_changed", _upstream_query, UPSTREAM, "query_names"),
        ("required_header_missing", _upstream_required_header, UPSTREAM, "header:x-api-key"),
    ],
}
_ALL = [(surface, *case) for surface, cases in SURFACE_CASES.items() for case in cases]


@pytest.mark.parametrize("surface,name,mutate,want_surface,want_path", _ALL, ids=[f"{c[0]}:{c[1]}" for c in _ALL])
def test_comparator_surface_mutation_detected(surface, name, mutate, want_surface, want_path):
    a, b = _pair()
    mutate(b)
    report = compare_captures(a, b)
    assert any(d.surface == want_surface and want_path in d.path for d in report.differences), report.text()


def test_contract_headers_are_covered():
    assert {h[0].lower() for h in CONTRACT_HEADERS} == {c[3] for c in SURFACE_CASES[HEADERS]}


# ---------------------------------------------------------------- 每條 15.9.5 規則


def _build(rule: PathRule, value: Any) -> Any:
    """依路徑造出最小 JSON,把 value 放在該路徑上(`[*]` 用單一項目的陣列,`*` 用鍵 `x`)。"""
    tokens = [t for t in rule.path.replace("[*]", ".[*]").replace("$.", "", 1).split(".") if t]
    node: Any = value
    for tok in reversed(tokens):
        if tok == "[*]":
            node = [node]
        elif tok == "*":
            node = {"x": node}
        else:
            node = {tok: node}
    return node


def _exchange(rule: PathRule, doc: Any) -> Exchange:
    method, path = rule.surface.split(" ", 1)
    return Exchange(method, path.replace("{id}", "x"), 200, _JSON, json.dumps(doc).encode())


def _env(side: str) -> Capture:
    return Capture(scenario="rule", port=51000 if side == "a" else 51001, home=f"/tmp/{side}", fake_base_url=f"http://127.0.0.1:{61000 if side == 'a' else 61001}",
                   corpus_keys=CORPUS_KEYS, tokens=("T" * 43,))


# kind → (a 的合法值, b 的合法值:應判相等, b 的不合法值)
def _samples(kind: str, cap_a: Capture, cap_b: Capture) -> tuple[Any, Any, Any]:
    return {
        "chat_id": ("chatcmpl-" + "a" * 32, "chatcmpl-" + "b" * 32, "chatcmpl-short"),
        "int": (5, 5, "5"),
        "int_or_null": (5, 5, 1.5),
        "token": ("T" * 43, "T" * 43, "short"),
        "port": (cap_a.port, cap_b.port, cap_b.port + 1),
        "home": (cap_a.home, cap_b.home, "/elsewhere"),
        "version": ("1.2.3", "9.9.9", "x"),
        "latency": (5, 99, -1),
        "presence": ("a", "b", ""),
        "number_only": (0.5, 9, "x"),
        "int_only": (5, 6, 1.5),
        "mcp_url": (f"http://127.0.0.1:{cap_a.port}/mcp/g", f"http://127.0.0.1:{cap_b.port}/mcp/g", "http://127.0.0.1:1/mcp/g"),
        "stdio_command": ("spillway", "spillway-rs", "other"),
    }[kind]


_VALUE_RULES = [r for r in PATH_RULES if r.surface != DB_SURFACE and r.kind not in ("api_base", "payload_request", "verbatim", "stdio_port")]


def _rule_id(r: PathRule) -> str:
    return f"{r.surface} {r.path}".replace(" ", "_")


@pytest.mark.parametrize("rule", _VALUE_RULES, ids=_rule_id)
def test_rule_equal_when_shape_valid_and_differs_when_not(rule):
    cap_a, cap_b = _env("a"), _env("b")
    good_a, good_b, bad = _samples(rule.kind, cap_a, cap_b)
    cap_a.exchanges, cap_b.exchanges = [_exchange(rule, _build(rule, good_a))], [_exchange(rule, _build(rule, good_b))]
    assert compare_captures(cap_a, cap_b).ok, rule
    cap_b.exchanges = [_exchange(rule, _build(rule, bad))]
    report = compare_captures(cap_a, cap_b)
    assert report.differences, rule
    assert any(d.kind == "shape" and d.side == "b" for d in report.differences), report.text()


@pytest.mark.parametrize("kind", ["chat_id", "stdio_command"])
def test_trailing_newline_breaks_response_shapes(kind):
    rule = next(r for r in _VALUE_RULES if r.kind == kind)
    cap_a, cap_b = _env("a"), _env("b")
    good, _, _ = _samples(kind, cap_a, cap_b)
    cap_a.exchanges, cap_b.exchanges = [_exchange(rule, _build(rule, good))], [_exchange(rule, _build(rule, good + "\n"))]
    report = compare_captures(cap_a, cap_b)
    assert any(d.kind == "shape" and d.side == "b" for d in report.differences), report.text()


def test_trailing_newline_breaks_token_shape_before_masking():
    rule = next(r for r in _VALUE_RULES if r.kind == "token")
    cap = _env("b")
    cap.exchanges = [_exchange(rule, _build(rule, "T" * 43 + "\n"))]
    assert ("$.token", "shape") in {(d.path, d.kind) for d in scan_capture(cap, "b")}


def test_trailing_newline_breaks_est_cost_shape():
    a, b = _pair()
    _set_header(b, "X-Spillway-Est-Cost", "0.00000123\n")
    report = compare_captures(a, b)
    assert any(d.surface == HEADERS and d.kind == "shape" and d.side == "b" for d in report.differences), report.text()


def test_trailing_newline_breaks_sha256_shape():
    a, b = _pair()
    b.db["settings"][0]["value"] = ("text", "b" * 64 + "\n")
    report = compare_captures(a, b)
    assert any(d.surface == DB_ROWS and d.kind == "shape" and d.side == "b" for d in report.differences), report.text()


@pytest.mark.parametrize("rule", [r for r in _VALUE_RULES if r.kind in ("int", "int_or_null")], ids=_rule_id)
def test_integer_rules_still_compare_value(rule):
    cap_a, cap_b = _env("a"), _env("b")
    cap_a.exchanges, cap_b.exchanges = [_exchange(rule, _build(rule, 5))], [_exchange(rule, _build(rule, 6))]
    report = compare_captures(cap_a, cap_b)
    assert any(d.kind == "value" for d in report.differences), report.text()


@pytest.mark.parametrize("kind_rule", [r for r in _VALUE_RULES if r.kind in ("latency", "presence", "number_only", "int_only")], ids=_rule_id)
def test_null_on_one_side_differs(kind_rule):
    cap_a, cap_b = _env("a"), _env("b")
    good_a, _, _ = _samples(kind_rule.kind, cap_a, cap_b)
    cap_a.exchanges, cap_b.exchanges = [_exchange(kind_rule, _build(kind_rule, good_a))], [_exchange(kind_rule, _build(kind_rule, None))]
    report = compare_captures(cap_a, cap_b)
    assert any(d.kind == "type" for d in report.differences), report.text()


API_BASE_RULES = [r for r in PATH_RULES if r.kind == "api_base" and r.surface != DB_SURFACE]


@pytest.mark.parametrize("rule", API_BASE_RULES, ids=_rule_id)
def test_api_base_rule(rule):
    cap_a, cap_b = _env("a"), _env("b")
    cap_a.exchanges = [_exchange(rule, _build(rule, cap_a.fake_base_url + "/v1"))]
    cap_b.exchanges = [_exchange(rule, _build(rule, cap_b.fake_base_url + "/v1"))]
    assert compare_captures(cap_a, cap_b).ok
    cap_b.exchanges = [_exchange(rule, _build(rule, "http://other/v1"))]
    assert not compare_captures(cap_a, cap_b).ok
    cap_a.exchanges = [_exchange(rule, _build(rule, "http://other/v1"))]
    assert compare_captures(cap_a, cap_b).ok


def test_db_api_base_rule():
    a, b = _pair()
    assert compare_captures(a, b).ok
    b.db["deployments"][0]["params"] = ("text", json.dumps({"api_base": "http://other/v1", "x": 1}, sort_keys=True))
    report = compare_captures(a, b)
    assert any(d.surface == DB_ROWS and "params" in d.path for d in report.differences), report.text()


# ---------------------------------------------------------------- 每條 15.8.3 規則(AM-10)


INT_1, INT_2 = ("integer", 1), ("integer", 2)
NULL = ("null", None)
# 既有測試已涵蓋的方式:(模組, 測試名)
DB_RULE_EXISTING_CASES = {
    "settings.access_token_sha256": (test_normalize, "test_db_settings_hash_shape_checked_not_reported_by_value"),
    "usage_payloads.request_json": (test_normalize, "test_db_request_json_verbatim_when_truncated"),
    "deployments.params": (sys.modules[__name__], "test_db_api_base_rule"),
}


def _db_rule_cases() -> list[tuple[str, str, Any, Any, Any, str, str]]:
    """(鍵, 案例名, a 的值, b 的合法值, b 的變動值, 預期 kind, 預期 side)"""
    how_to_cases: dict[str, list[tuple[str, Any, Any, Any, str, str]]] = {
        "literal": [("one_char", ("text", "abc"), ("text", "abc"), ("text", "abd"), "value", "")],
        "integer": [("value", INT_1, INT_1, INT_2, "value", ""), ("text_type", INT_1, INT_1, ("text", "1"), "shape", "b")],
        "latency": [("negative", ("integer", 5), ("integer", 99), ("integer", -1), "shape", "b"),
                    ("real", ("integer", 5), ("integer", 99), ("real", 1.5), "shape", "b"),
                    ("null", ("integer", 5), ("integer", 99), NULL, "typeof", "")],
        "presence": [("empty", ("text", "a"), ("text", "b"), ("text", ""), "shape", "b"),
                     ("null", ("text", "a"), ("text", "b"), NULL, "typeof", "")],
        "type_only": [("type", ("real", 0.5), ("real", 1.5), ("text", "x"), "typeof", "")],
    }
    return [(key, *case) for key, how in DB_COLUMN_RULES.items() if key not in DB_RULE_EXISTING_CASES for case in how_to_cases[how]]


_DB_CASES = _db_rule_cases()


def _put_cell(cap: Capture, key: str, cell: Any) -> None:
    """把欄位設到第一列;sample 沒有的表(`groups`、`routes`)一併建立。"""
    table, col = key.split(".", 1)
    if table == "settings":
        name = "other_setting" if col == "*" else col
        cap.db["settings"].append({"key": ("text", name), "value": cell})
        return
    cap.db.setdefault(table, [{}])[0][col] = cell


@pytest.mark.parametrize("key,name,value_a,good_b,bad_b,want_kind,want_side", _DB_CASES, ids=[f"{c[0]}:{c[1]}" for c in _DB_CASES])
def test_db_column_rule_mutations(key, name, value_a, good_b, bad_b, want_kind, want_side):
    a, b = _pair()
    for cap in (a, b):      # sample 兩側的 request_json 只有在 truncated = 0 時才相等
        cap.db["usage_payloads"][0]["request_json"] = ("text", '{"a": 1}')
    _put_cell(a, key, value_a)
    _put_cell(b, key, good_b)
    report = compare_captures(a, b)
    assert report.ok, report.text()
    if key.startswith("settings."):
        b.db["settings"].pop()
    _put_cell(b, key, bad_b)
    report = compare_captures(a, b)
    col = "value" if key.startswith("settings.") else key.split(".", 1)[1]
    assert (DB_ROWS, col, want_kind, want_side) in {(d.surface, d.path, d.kind, d.side) for d in report.differences}, report.text()


def test_every_db_rule_has_a_case():
    assert {c[0] for c in _DB_CASES} | set(DB_RULE_EXISTING_CASES) == set(DB_COLUMN_RULES)
    for module, name in DB_RULE_EXISTING_CASES.values():
        assert callable(getattr(module, name))


def test_upstream_has_json_on_one_side_differs():
    a, b = _pair()
    b.upstream[0].has_json, b.upstream[0].json_body = False, None
    report = compare_captures(a, b)
    assert (UPSTREAM, "$", "type") in {(d.surface, d.path, d.kind) for d in report.differences}, report.text()


def test_stdio_port_rule():
    rule = next(r for r in PATH_RULES if r.kind == "stdio_port")
    def doc(port: Any) -> dict:
        return {"connection": {"stdio": {"args": ["mcp-stdio", "--port", port]}}}
    cap_a, cap_b = _env("a"), _env("b")
    cap_a.exchanges = [_exchange(rule, doc(str(cap_a.port)))]
    cap_b.exchanges = [_exchange(rule, doc(str(cap_b.port)))]
    assert compare_captures(cap_a, cap_b).ok
    cap_b.exchanges = [_exchange(rule, doc("1"))]
    report = compare_captures(cap_a, cap_b)
    assert any(d.kind == "shape" for d in report.differences), report.text()


def test_payload_request_rule():
    rule = next(r for r in PATH_RULES if r.kind == "payload_request")
    cap_a, cap_b = _env("a"), _env("b")
    cap_a.exchanges = [_exchange(rule, {"request_json": '{"a": 1, "b": 2}', "truncated": False})]
    cap_b.exchanges = [_exchange(rule, {"request_json": '{"b":2,"a":1}', "truncated": False})]
    assert compare_captures(cap_a, cap_b).ok
    cap_b.exchanges = [_exchange(rule, {"request_json": '{"b":3,"a":1}', "truncated": False})]
    assert not compare_captures(cap_a, cap_b).ok
    # 一側不能解析時不得視為相等
    cap_b.exchanges = [_exchange(rule, {"request_json": "not json", "truncated": False})]
    assert not compare_captures(cap_a, cap_b).ok


def test_response_text_verbatim_rule():
    rule = next(r for r in PATH_RULES if r.kind == "verbatim")
    cap_a, cap_b = _env("a"), _env("b")
    cap_a.exchanges = [_exchange(rule, {"response_text": "x"})]
    cap_b.exchanges = [_exchange(rule, {"response_text": "y"})]
    assert not compare_captures(cap_a, cap_b).ok


# ---------------------------------------------------------------- 金鑰與 sentinel


def _key_in_field(b: Capture) -> None:
    _edit_body(b, 0, lambda d: d.__setitem__("leak", KEY))


def test_key_in_response_field_is_inconsistent():
    a, b = _pair()
    _key_in_field(b)
    report = compare_captures(a, b)
    assert any(d.kind == "sentinel" and d.path == "$.leak" for d in report.differences), report.text()
    assert KEY not in report.text()


def test_sentinel_on_one_side_while_other_side_masked():
    a, b = _pair()
    _edit_body(a, 0, lambda d: d["choices"][0]["message"].__setitem__("content", MASK))
    _edit_body(b, 0, lambda d: d["choices"][0]["message"].__setitem__("content", KEY))
    report = compare_captures(a, b)
    assert [(d.kind, d.side) for d in report.differences if d.kind == "sentinel"] == [("sentinel", "b")], report.text()


@pytest.mark.parametrize("name,place", [
    ("header", lambda b: _set_header(b, "x-odd", KEY)),
    ("db_text", lambda b: b.db["settings"][1].__setitem__("value", ("text", KEY))),
    ("error_message", lambda b: _edit_body(b, 6, lambda d: d["error"].__setitem__("message", f"x {KEY} y"))),
])
def test_sentinel_detected_at_every_scan_surface(name, place):
    a, b = _pair()
    place(b)
    report = compare_captures(a, b)
    assert any(d.kind == "sentinel" for d in report.differences), report.text()
    assert KEY not in report.text()


def test_sentinel_in_non_json_body():
    a, b = _pair()
    b.exchanges[7] = replace(b.exchanges[7], body=KEY.encode())
    report = compare_captures(a, b)
    assert any(d.kind == "sentinel" for d in report.differences), report.text()


# ---------------------------------------------------------------- 端到端(線上內容;經 mutant_wrapper)


def _by_name(name: str) -> Any:
    return next(s for s in SCENARIOS if s.name == name)


def _paths(report: Report) -> set[tuple[str, str]]:
    return {(d.surface, d.path) for d in report.differences}


def test_wrapper_without_mutation_has_no_differences():
    for name in ("headers", "json_body", "db_rows"):
        result = run_pair(_by_name(name), cmd_b=mutant_cmd("none"))
        assert result.report.ok, result.report.text()


def test_second_implementation_taken_from_env(monkeypatch):
    """第二實作由 `SPILLWAY_PARITY_CMD_B` 指定(AM-9)。"""
    monkeypatch.setenv(CMD_B_ENV, shlex.join(mutant_cmd("status")))
    result = run_pair(_by_name("headers"))
    assert (STATUS, "status") in _paths(result.report), result.report.text()


def test_wrapper_status_mutation_detected():
    result = run_pair(_by_name("headers"), cmd_b=mutant_cmd("status"))
    assert (STATUS, "status") in _paths(result.report), result.report.text()


def test_wrapper_header_mutation_detected():
    result = run_pair(_by_name("headers"), cmd_b=mutant_cmd("header"))
    assert (HEADERS, "x-spillway-served-by") in _paths(result.report), result.report.text()


def test_wrapper_json_body_mutation_detected_with_only_that_path():
    result = run_pair(_by_name("json_body"), cmd_b=mutant_cmd("json_body"))
    assert (JSON_BODY, "$.choices[0].message.content") in _paths(result.report), result.report.text()
    assert {d.path for d in result.report.differences} == {"$.choices[0].message.content"}, result.report.text()


def test_wrapper_db_row_mutation_detected():
    result = run_pair(_by_name("db_rows"), cmd_b=mutant_cmd("db_row"))
    assert (DB_ROWS, "tokens_in") in _paths(result.report), result.report.text()


def test_wrapper_key_in_response_mutation_detected():
    result = run_pair(_by_name("json_body"), cmd_b=mutant_cmd("key_in_response"))
    assert any(d.kind == "sentinel" and d.path == "$.leaked" for d in result.report.differences), result.report.text()
    assert KEY not in result.report.text()

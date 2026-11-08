"""秘密不外洩(CHG-20261108-01 T3 L4;AM-8;spec 15.10):報告、junit、runner 記錄檔位元組掃描 sentinel 0 命中;log 行的 sentinel 與等級格式。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from tests.parity.corpus import CORPUS_KEYS, SAMPLE_TOKEN, SCENARIOS, FakeMode, Step, sample_capture
from tests.parity.normalize import LOG, Exchange, compare_captures, scan_capture, sentinels_of
from tests.parity.runner import PairResult, _log_lines, mutant_cmd, run_pair, run_scenario

REPO = Path(__file__).resolve().parents[2]
KEY = CORPUS_KEYS[0]
SCANNED = (*CORPUS_KEYS, SAMPLE_TOKEN)


# ---------------------------------------------------------------- 語料


def test_corpus_keys_use_test_prefixes():
    assert all(k.startswith(("sk-test-", "sk-fake")) for k in CORPUS_KEYS)


def test_echo_auth_is_not_in_the_corpus():
    modes = {s.mode for scenario in SCENARIOS for s in scenario.steps if isinstance(s, FakeMode)}
    assert "echo_auth" not in modes


def test_corpus_scope():
    """15.11:不含 estimate、dry-run、dashboard、adapters;不可達上游只用固定位址。"""
    paths = {s.path for scenario in SCENARIOS for s in scenario.steps if hasattr(s, "path")}
    assert not any(p.startswith(("/api/estimate", "/api/dashboard", "/api/adapters")) or "/dry-run" in p for p in paths)
    bases = [s.json["params"].get("api_base") for sc in SCENARIOS for s in sc.steps
             if getattr(s, "path", "") == "/api/deployments" and getattr(s, "method", "") == "POST"]
    assert all(b.startswith("$FAKE_BASE") or b == "http://127.0.0.1:9/v1" for b in bases)


def test_corpus_limits_budget_and_cloud_candidates_per_slot():
    """15.11:每個 slot 至多一個雲端候選;不設 `budget_precheck`、`usd` 上限。"""
    for scenario in SCENARIOS:
        creates = [s.json for s in scenario.steps if getattr(s, "path", "") == "/api/deployments" and getattr(s, "method", "") == "POST"]
        kinds = {b["id"]: b["kind"] for b in creates}
        assert not any((b.get("budget") or {}).get("type") == "usd" for b in creates), scenario.name
        for step in scenario.steps:
            body = getattr(step, "json", None)
            if isinstance(body, dict):
                assert not body.get("budget_precheck"), scenario.name
                for slot in body.get("slots") or []:
                    assert sum(kinds[e["deployment"]] in ("cli", "api") for e in slot) <= 1, scenario.name


# ---------------------------------------------------------------- 報告


def _report_with_secrets():
    a, b = sample_capture("a"), sample_capture("b")
    b.exchanges[6] = replace(b.exchanges[6], path=f"/api/deployments/{KEY}", body=json.dumps({KEY: "x"}).encode())
    b.exchanges.append(replace(b.exchanges[0], headers=((KEY, KEY),)))
    b.db["usage_ledger"][0]["error"] = ("text", KEY)
    b.log_lines.append(f"SPILLWAY-LOG INFO mod {KEY}")
    return compare_captures(a, b)


def test_report_never_contains_sentinels():
    report = _report_with_secrets()
    assert any(d.kind == "sentinel" for d in report.differences)
    assert KEY not in report.text()
    assert not any(KEY in str(d) for d in report.differences)


def test_settings_hash_not_in_report():
    a, b = sample_capture("a"), sample_capture("b")
    hash_a = a.db["settings"][0]["value"][1]
    b.db["settings"][0]["value"] = ("text", "z" * 64)
    report = compare_captures(a, b)
    assert not report.ok and hash_a not in report.text() and "z" * 64 not in report.text()


# ---------------------------------------------------------------- log 行


def test_sentinel_in_log_line_is_inconsistent():
    a, b = sample_capture("a"), sample_capture("b")
    b.log_lines.append(f"SPILLWAY-LOG INFO spillway.x leaked {KEY}")
    report = compare_captures(a, b)
    assert [(d.surface, d.kind, d.side) for d in report.differences] == [("log", "sentinel", "b")], report.text()
    assert KEY not in report.text()


def test_sentinel_in_log_continuation_line_is_inconsistent():
    a, b = sample_capture("a"), sample_capture("b")
    b.log_lines.append(f"SPILLWAY-LOG-CONT {KEY}")
    report = compare_captures(a, b)
    assert [(d.surface, d.kind) for d in report.differences] == [("log", "sentinel")], report.text()


@pytest.mark.parametrize("line", ["SPILLWAY-LOG NOTICE mod message", "SPILLWAY-LOG INFO", "SPILLWAY-LOG"])
def test_invalid_log_level_format_is_inconsistent(line):
    a, b = sample_capture("a"), sample_capture("b")
    b.log_lines.append(line)
    report = compare_captures(a, b)
    assert [(d.surface, d.kind) for d in report.differences] == [("log", "log_format")], report.text()


def test_bare_log_marker_line_is_kept():
    assert _log_lines(b"SPILLWAY-LOG\n") == ["SPILLWAY-LOG"]


def test_log_line_count_and_message_not_compared():
    a, b = sample_capture("a"), sample_capture("b")
    b.log_lines.extend(["SPILLWAY-LOG WARNING other.logger different message", "SPILLWAY-LOG DEBUG x y"])
    assert compare_captures(a, b).ok


# ---------------------------------------------------------------- junit、stdout、記錄檔位元組


INNER_TEST = """
import json
from dataclasses import replace
from tests.parity.corpus import CORPUS_KEYS, SCENARIOS, Step, sample_capture
from tests.parity.normalize import compare_captures
from tests.parity.runner import PairResult, _do_step, mutant_cmd, run_pair

KEY = CORPUS_KEYS[0]


def _secret_scenario():
    return next(s for s in SCENARIOS if s.name == "secret_in_prompt")

def test_comparator_report():
    a, b = sample_capture("a"), sample_capture("b")
    b.exchanges[6] = replace(b.exchanges[6], body=json.dumps({"error": {"code": KEY, "message": KEY}}).encode())
    report = compare_captures(a, b)
    assert report.ok, report.text()

def test_live_key_in_response_report():
    scenario = next(s for s in SCENARIOS if s.name == "json_body")
    result = run_pair(scenario, cmd_b=mutant_cmd("key_in_response"))
    assert result.report.ok, result.report.text()

def test_pair_result_is_expanded_by_pytest():
    a, b = sample_capture("a"), sample_capture("b")
    b.exchanges[0] = replace(b.exchanges[0], status=201)
    result = PairResult(_secret_scenario(), compare_captures(a, b), [], [], a, b)
    assert result.report.ok

class RaisingClient:
    def request(self, *args, **kwargs):
        __tracebackhide__ = True    # 這一層的 kwargs 是測試替身自己的參數
        raise RuntimeError("request failed")

def test_do_step_funcargs_are_printed():
    step = next(s for s in _secret_scenario().steps if isinstance(s, Step) and s.json and KEY in json.dumps(s.json))
    _do_step(step, RaisingClient(), "http://127.0.0.1:1", sample_capture("a"))
"""


@pytest.mark.parametrize("flag", ["-q", "-vv", "--tb=long"])
def test_failure_outputs_contain_no_sentinels(tmp_path, flag):
    root = tmp_path / "inner"
    root.mkdir()
    (root / "test_inner.py").write_text(INNER_TEST, encoding="utf-8")
    junit = root / "junit.xml"
    env = {k: v for k, v in os.environ.items() if k not in ("SPILLWAY_IMPL", "SPILLWAY_CMD", "SPILLWAY_BLACKBOX_GUARD")}
    env["PYTHONPATH"] = str(REPO)
    proc = subprocess.run([sys.executable, "-m", "pytest", flag, "-p", "no:cacheprovider", "--rootdir", str(root), f"--junitxml={junit}",
                           str(root / "test_inner.py")], cwd=root, env=env, capture_output=True, timeout=180)
    assert proc.returncode == 1, proc.stdout[-1000:]      # 全部案例都失敗
    assert b"sentinel" in junit.read_bytes()              # 確實跑到判定,不是提早失敗
    for name, data in (("junit", junit.read_bytes()), ("stdout", proc.stdout), ("stderr", proc.stderr)):
        for secret in SCANNED:
            assert secret.encode() not in data, f"{name} 含金鑰或 token 值"


@pytest.mark.parametrize("name", ["secret_in_prompt", "upstream_failures", "token_rotate"])
def test_runner_log_file_contains_no_sentinels(name):
    scenario = next(s for s in SCENARIOS if s.name == name)
    root = Path(tempfile.mkdtemp(prefix="spillway-parity-keep-")).resolve()
    try:
        capture = run_scenario(scenario, keep_root=root)
        data = (root / "serve.log").read_bytes()
        assert data, "記錄檔不應為空"
        for secret in (*CORPUS_KEYS, *capture.tokens):
            assert secret.encode() not in data, "serve 記錄檔含秘密值"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_key_written_to_stderr_is_sentinel_and_scrubbed_from_log_file():
    scenario = next(s for s in SCENARIOS if s.name == "json_body")
    root = Path(tempfile.mkdtemp(prefix="spillway-parity-keep-")).resolve()
    try:
        capture = run_scenario(scenario, cmd=mutant_cmd("log_key"), keep_root=root)
        assert (LOG, "sentinel") in {(d.surface, d.kind) for d in scan_capture(capture, "b")}
        data = (root / "serve.log").read_bytes()
        assert data, "記錄檔不應為空"
        for key in CORPUS_KEYS:
            assert key.encode() not in data, "serve 記錄檔含語料金鑰"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_mixed_case_secret_in_header_name_not_reported():
    mixed = "MiXeDcAsEtOkEn0123456789abcdefGHIJKLMNOPQRS"
    a, b = sample_capture("a"), sample_capture("b")
    for cap in (a, b):
        cap.tokens = (SAMPLE_TOKEN, mixed)
    b.exchanges[0] = replace(b.exchanges[0], headers=(*b.exchanges[0].headers, (f"x-{mixed}", "v")))
    b.upstream[0].header_names = (*b.upstream[0].header_names, f"x-{mixed.lower()}")
    report = compare_captures(a, b)
    assert report.differences
    assert mixed not in report.text() and mixed.lower() not in report.text()


def test_reprs_contain_no_sentinels():
    a, b = sample_capture("a"), sample_capture("b")
    exchange = Exchange("GET", "/x", 200, (("x-leak", KEY),), json.dumps({"k": KEY}).encode())
    a.exchanges.append(exchange)
    a.log_lines.append(f"SPILLWAY-LOG INFO mod {KEY}")
    scenario = next(s for s in SCENARIOS if s.name == "secret_in_prompt")
    seeded = next(s for s in SCENARIOS if s.name == "seeded_params")
    steps = [s for sc in (scenario, seeded) for s in sc.steps if isinstance(s, Step) and s.json and any(k in json.dumps(s.json) for k in CORPUS_KEYS)]
    assert len(steps) >= 2
    result = PairResult(scenario, compare_captures(a, b), [], [], a, b)
    for obj in (*steps, scenario, seeded, exchange, a, result, sentinels_of(a)):
        text = repr(obj)
        assert not any(s in text for s in SCANNED), type(obj).__name__


def test_live_key_in_response_is_reported_without_value():
    scenario = next(s for s in SCENARIOS if s.name == "json_body")
    result = run_pair(scenario, cmd_b=mutant_cmd("key_in_response"))
    assert any(d.kind == "sentinel" and d.side == "b" for d in result.report.differences), result.report.text()
    assert KEY not in result.report.text()

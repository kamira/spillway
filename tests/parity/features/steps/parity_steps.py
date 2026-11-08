"""`tests/parity/features/parity.feature` 的步驟(CHG-20261108-01 T3;使用者故事 2、3)。

差分比對透過 `tests.parity.runner`(實際啟動 serve)。差距清單步驟沿用 `test_gaps_file.py` 的內層 pytest 夾具,驗證的是 T2 的正式機制。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from behave import given, then, when

from tests.parity import gaps
from tests.parity.corpus import CORPUS_KEYS
from tests.parity.runner import mutant_cmd, run_corpus
from tests.parity.test_gaps_file import _gap_toml, _inner


def _results_text(context) -> str:
    return "\n".join(r.report.text() for r in context.results)


# ---------------------------------------------------------------- 差分比對


@given("第一實作與第二實作都是 Python 版")
def step_both_python(context):
    context.cmd_b = None


@given("第一實作是 Python 版,第二實作是會改動回應 JSON body 的突變 wrapper")
def step_mutant(context):
    context.cmd_b = mutant_cmd("json_body")


@when("以語料的每個情境分別操作兩個實作")
def step_run_corpus(context):
    context.results = run_corpus(cmd_b=context.cmd_b)


@then("每個情境宣告的比對面都有內容")
def step_surfaces_non_empty(context):
    empty = {r.scenario.name: (r.empty_a, r.empty_b) for r in context.results if r.empty_a or r.empty_b}
    assert not empty, empty


@then("依 spec 15 比對的差異為 0 項")
def step_no_differences(context):
    assert all(r.report.ok for r in context.results), _results_text(context)


@then("比對結果不一致")
def step_inconsistent(context):
    assert any(not r.report.ok for r in context.results), "突變未被判定不一致"


@then("差異報告指出 JSON body 的欄位路徑")
def step_reports_json_path(context):
    hits = [d for r in context.results for d in r.report.differences
            if d.surface == "json_body" and d.path == "$.choices[0].message.content"]
    assert hits, _results_text(context)


@then("差異報告不含金鑰或 token 值")
def step_report_clean(context):
    secrets = {*CORPUS_KEYS, *(t for r in context.results for t in (*r.capture_a.tokens, *r.capture_b.tokens))}
    text = _results_text(context)
    leaked = sum(1 for s in secrets if s in text)
    assert leaked == 0, f"差異報告含 {leaked} 個秘密值"


# ---------------------------------------------------------------- 差距清單


@given("差距清單列了一個實際會通過的測試")
def step_gap_passes(context):
    context.gap_id = "test_sample.py::test_passes"
    context.gap_toml = _gap_toml(context.gap_id)


@when("以 SPILLWAY_IMPL=rust 套用差距清單執行該測試")
def step_run_gap(context):
    tmp = Path(tempfile.mkdtemp(prefix="spillway-parity-gap-")).resolve()
    context.add_cleanup(shutil.rmtree, tmp, True)
    context.inner = _inner(tmp, context.gap_toml, sample="def test_passes():\n    pass\n")


@then("該次執行判定失敗,並指出要刪除的清單項")
def step_gap_failed(context):
    proc = context.inner
    assert proc.returncode == 1, proc.stdout[-1500:]
    assert "XPASS(strict)" in proc.stdout and context.gap_id in proc.stdout, proc.stdout[-1500:]


PROTECTED_ID = "tests/blackbox/test_control_contract.py::test_off_every_method_and_variant_is_404_like_unknown_path"


@given("差距清單列了一個受保護清單內的測試")
def step_gap_protected(context):
    tmp = Path(tempfile.mkdtemp(prefix="spillway-parity-protected-")).resolve()
    context.add_cleanup(shutil.rmtree, tmp, True)
    context.gap_file = tmp / "gaps.toml"
    context.gap_file.write_text(_gap_toml(PROTECTED_ID), encoding="utf-8")


@when("檢查差距清單檔案")
def step_check_protected(context):
    expanded, _ = gaps.expand_protected(gaps.load_protected(gaps.PROTECTED_FILE), {PROTECTED_ID})
    protected = {i for ids in expanded.values() for i in ids}
    context.hits = gaps.protected_hits(gaps.load_gaps(context.gap_file), protected)


@then("檢查失敗,並指出該測試 id")
def step_protected_failed(context):
    assert context.hits == [PROTECTED_ID], context.hits

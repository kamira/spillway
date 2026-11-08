"""本機閘結果彙整(CHG-20261108-01 T4;AM-11):pytest 結果 plugin、`artifacts/gate/<sha>.json` 的內容、`results.py` 的 rust 子目錄。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.acceptance import results
from tests.parity import gate_report
from tests.parity.corpus import CORPUS_KEYS

REPO = Path(__file__).resolve().parents[2]
SHA = "a" * 40

INNER = """
import pytest

@pytest.fixture
def broken_setup():
    raise RuntimeError("setup")

@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("teardown")

def test_pass(): pass
def test_fail(): assert False
def test_skip(): pytest.skip("x")
@pytest.mark.xfail
def test_xfail(): assert False
@pytest.mark.xfail
def test_xpass(): pass
def test_setup_error(broken_setup): pass
def test_teardown_error(broken_teardown): pass
@pytest.mark.parametrize("v", [1])
def test_param(v): pass
"""


def _inner_outcomes(tmp_path: Path, files: dict[str, str]) -> dict:
    root = tmp_path / "inner"
    root.mkdir()
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    out = tmp_path / "results.json"
    env = {k: v for k, v in os.environ.items() if k not in ("SPILLWAY_IMPL", "SPILLWAY_CMD", "SPILLWAY_BLACKBOX_GUARD")}
    env["PYTHONPATH"] = str(REPO)
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(root), "-p", "tests.parity.gate_report",
                           f"--gate-results={out}", str(root)], cwd=root, env=env, capture_output=True, timeout=120)
    assert proc.returncode in (1, 2), proc.stdout[-1000:]      # 有失敗 1、收集錯誤 2
    return json.loads(out.read_text(encoding="utf-8"))["outcomes"]


def test_plugin_records_every_status_by_node_id(tmp_path):
    assert _inner_outcomes(tmp_path, {"test_a.py": INNER}) == {
        "test_a.py::test_pass": "passed",
        "test_a.py::test_fail": "failed",
        "test_a.py::test_skip": "skipped",
        "test_a.py::test_xfail": "xfailed",
        "test_a.py::test_xpass": "xpassed",
        "test_a.py::test_setup_error": "error",
        "test_a.py::test_teardown_error": "error",
        "test_a.py::test_param[1]": "passed",
    }


def test_plugin_records_collection_errors(tmp_path):
    assert _inner_outcomes(tmp_path, {"test_bad.py": "raise RuntimeError('x')\n"}) == {"test_bad.py": "error"}


def _pytest_part(tmp_path: Path, name: str, outcomes: dict[str, str], code: int = 0) -> gate_report.Part:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"outcomes": outcomes}), encoding="utf-8")
    return gate_report.Part(name, "pytest", code, 3, path)


def _behave_part(tmp_path: Path, name: str, statuses: dict[str, str], code: int = 0) -> gate_report.Part:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps([{"location": "tests/features/a.feature:1", "status": "passed",
                                 "elements": [{"type": "scenario", "name": n, "status": s, "steps": []} for n, s in statuses.items()]}]),
                    encoding="utf-8")
    return gate_report.Part(name, "behave", code, 2, path)


def _green_parts(tmp_path: Path) -> list[gate_report.Part]:
    return [_pytest_part(tmp_path, "pytest_full", {"t.py::a": "passed", "t.py::b": "skipped"}),
            _behave_part(tmp_path, "behave_features", {"s1": "passed", "s2": "skipped"}),
            gate_report.Part("compare", "plain", 0, 1, None)]


def test_report_has_required_fields(tmp_path):
    report = gate_report.build_report(_green_parts(tmp_path), SHA, dirty=False, gap_count=0)
    assert report["git_sha"] == SHA and report["git_dirty"] is False
    assert report["result"] == "pass"
    assert report["gap_count"] == 0
    assert report["test_ids"]["first"] == {"pytest": ["t.py::a", "t.py::b"],
                                           "behave": ["tests/features/a.feature::s1", "tests/features/a.feature::s2"]}
    parts = {p["name"]: p for p in report["parts"]}
    assert parts["pytest_full"]["counts"] == {"passed": 1, "skipped": 1}
    assert parts["pytest_full"]["skipped"] == ["t.py::b"]
    assert parts["behave_features"]["skipped"] == ["tests/features/a.feature::s2"]
    assert parts["compare"]["passed"] is True


def test_report_states_no_rust_side(tmp_path):
    report = gate_report.build_report(_green_parts(tmp_path), SHA, dirty=False, gap_count=0)
    assert report["rust_side"]["exists"] is False
    assert report["implementations"]["first"]["label"] == "python"
    second = report["implementations"]["second"]
    assert "rust" not in second["label"].lower() and "mutant_wrapper" in second["label"]
    assert report["test_ids"]["second"] is None
    assert report["id_differences"]["compared"] is False


@pytest.mark.parametrize("index", [0, 1, 2])
def test_any_failing_part_fails_the_report(tmp_path, index):
    parts = _green_parts(tmp_path)
    parts[index] = parts[index]._replace(exit_code=1)
    report = gate_report.build_report(parts, SHA, dirty=False, gap_count=0)
    assert report["result"] == "fail" and not report["parts"][index]["passed"]
    assert sum(p["passed"] for p in report["parts"]) == 2


@pytest.mark.parametrize("kind", ["pytest", "behave"])
def test_green_exit_without_results_is_a_failure(tmp_path, kind):
    report = gate_report.build_report([gate_report.Part("x", kind, 0, 1, tmp_path / "missing.json")], SHA, dirty=False, gap_count=0)
    assert report["result"] == "fail" and report["parts"][0]["results_present"] is False


def test_failed_ids_are_listed_without_messages(tmp_path):
    part = _pytest_part(tmp_path, "pytest_full", {"t.py::a": "failed", "t.py::b": "error", "t.py::c": "passed"}, code=1)
    report = gate_report.build_report([part], SHA, dirty=False, gap_count=0)
    assert report["parts"][0]["failed"] == ["t.py::a", "t.py::b"]


def test_registered_differences_pass_and_unregistered_fail(tmp_path):
    parts = [_pytest_part(tmp_path, "pytest_full", {"t.py::a": "passed", "t.py::only_python[1]": "passed"})]
    second = {"pytest": {"t.py::a", "t.py::only_rust"}, "behave": set()}
    registered = ["t.py::only_python", "t.py::only_rust"]
    report = gate_report.build_report(parts, SHA, dirty=False, gap_count=2, second_ids=second, registered=registered)
    assert report["id_differences"]["compared"] is True and report["id_differences"]["unregistered"] == []
    assert report["result"] == "pass"
    report = gate_report.build_report(parts, SHA, dirty=False, gap_count=2, second_ids=second, registered=["t.py::only_python"])
    assert report["id_differences"]["unregistered"] == ["t.py::only_rust"]
    assert report["result"] == "fail"


def _cli(tmp_path: Path, parts: list[gate_report.Part]) -> tuple[int, Path]:
    table = tmp_path / "parts.tsv"
    table.write_text("".join(f"{p.name}\t{p.kind}\t{p.exit_code}\t{p.seconds}\t{p.results or ''}\n" for p in parts), encoding="utf-8")
    out = tmp_path / "gate" / f"{SHA}.json"
    return gate_report.main(["--sha", SHA, "--dirty", "0", "--parts", str(table), "--out", str(out)]), out


def test_cli_writes_report_and_exit_code(tmp_path, capsys):
    code, out = _cli(tmp_path, _green_parts(tmp_path))
    assert code == 0
    assert json.loads(out.read_text(encoding="utf-8"))["gap_count"] == 0     # rust_gaps.toml 本筆為空
    assert "skipped:t.py::b" in capsys.readouterr().out


def test_cli_exit_one_when_a_part_failed(tmp_path):
    parts = _green_parts(tmp_path)
    parts[2] = parts[2]._replace(exit_code=1)
    code, out = _cli(tmp_path, parts)
    assert code == 1 and json.loads(out.read_text(encoding="utf-8"))["result"] == "fail"


def test_cli_refuses_to_write_sentinels(tmp_path):
    parts = [_pytest_part(tmp_path, "pytest_full", {f"t.py::a[{CORPUS_KEYS[0]}]": "passed"})]
    code, out = _cli(tmp_path, parts)
    assert code == 2 and not out.exists()


@pytest.mark.parametrize("impl,expected", [("python", "acceptance-results.json"), ("rust", "rust/acceptance-results.json")])
def test_acceptance_results_use_separate_directory_for_rust(tmp_path, monkeypatch, impl, expected):
    monkeypatch.setattr(results, "ARTIFACTS", tmp_path)
    monkeypatch.setenv("SPILLWAY_IMPL", impl)
    monkeypatch.setenv("SPILLWAY_CMD", "/nonexistent/spillway-impl") if impl == "rust" else monkeypatch.delenv("SPILLWAY_CMD", raising=False)
    results.record_result("k", 1)
    assert json.loads((tmp_path / expected).read_text(encoding="utf-8")) == {"k": 1}
    assert [p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.json")] == [expected]

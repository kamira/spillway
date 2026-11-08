"""本機閘的結果彙整(CHG-20261108-01 T4;AM-11)。不 import spillway,不讀環境變數。

兩個用途,都由 `scripts/local_gate.sh` 呼叫:

- pytest plugin:`-p tests.parity.gate_report --gate-results=FILE`,以 node ID 記錄每個測試的結果。用命令列選項而不是環境變數:
  測試自己開的內層 pytest 不會繼承。
- 命令列:`python -m tests.parity.gate_report --sha SHA --dirty 0|1 --parts PARTS.tsv --out FILE`,寫出 `artifacts/gate/<sha>.json`。
  結束碼 0 全部通過、1 有一段未通過、2 輸入不合法或報告含 sentinel(此時不寫檔)。

報告與 stdout 只有測試 id、狀態與計數,沒有錯誤內容(15.10.4)。本筆沒有 Rust 程式,第二側是包裝同一份 Python 的突變 wrapper,
沒有獨立的測試 id 集合,報告明寫 `rust_side.exists = false`。`build_report` 的 `second_ids`、`registered` 留給之後實際跑兩側的閘。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Collection, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

import pytest

FORMAT = "spillway-local-gate/1"
_NOT_FAILED = ("passed", "skipped", "xfailed", "xpassed")
_RESULTS_KEY = pytest.StashKey[dict[str, str]]()


# ---------------------------------------------------------------- pytest plugin


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--gate-results", default=None, help="把每個測試的結果以 node ID 寫成 JSON(scripts/local_gate.sh 使用)")


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("--gate-results"):
        config.stash[_RESULTS_KEY] = {}
        config.pluginmanager.register(_Recorder(config), "spillway-gate-recorder")


def _outcome(report: pytest.TestReport) -> str | None:
    """這個階段決定的狀態;None 表示這個階段不影響結果。teardown 失敗蓋過 call 的結果。"""
    xfail = hasattr(report, "wasxfail")
    if report.failed:
        return "failed" if report.when == "call" else "error"
    if report.skipped:
        return "xfailed" if xfail else "skipped"
    if report.when == "call":
        return "xpassed" if xfail else "passed"
    return None


class _Recorder:
    def __init__(self, config: pytest.Config) -> None:
        self.path = Path(config.getoption("--gate-results")).resolve()
        self.outcomes = config.stash[_RESULTS_KEY]

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        outcome = _outcome(report)
        if outcome is not None:
            self.outcomes[report.nodeid] = outcome

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self.outcomes[report.nodeid or "<collection>"] = "error"

    def pytest_sessionfinish(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"outcomes": self.outcomes}, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- 報告


class Part(NamedTuple):
    name: str
    kind: str                   # pytest、behave、plain(只有結束碼)
    exit_code: int
    seconds: int
    results: Path | None


def _load_outcomes(part: Part) -> dict[str, str] | None:
    """讀該段的結果檔;檔案不存在或不可用時回傳 None。"""
    from tests.parity.behave_gate import ReportError, scenario_outcomes

    if part.results is None:
        return None
    try:
        data = json.loads(part.results.read_text(encoding="utf-8"))
        if part.kind == "pytest":
            return {str(k): str(v) for k, v in data["outcomes"].items()}
        return {k: o.status for k, o in scenario_outcomes(data).items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ReportError):
        return None


def _part_entry(part: Part, outcomes: dict[str, str] | None) -> dict[str, Any]:
    needs_results = part.kind != "plain"
    found = outcomes or {}
    counts: dict[str, int] = {}
    for status in found.values():
        counts[status] = counts.get(status, 0) + 1
    return {
        "name": part.name,
        "kind": part.kind,
        "exit_code": part.exit_code,
        "seconds": part.seconds,
        "passed": part.exit_code == 0 and (not needs_results or bool(found)),
        "results_present": bool(found) if needs_results else None,
        "total": len(found),
        "counts": dict(sorted(counts.items())),
        "failed": sorted(i for i, s in found.items() if s not in _NOT_FAILED),
        "skipped": sorted(i for i, s in found.items() if s == "skipped"),
    }


def _is_registered(test_id: str, registered: Iterable[str]) -> bool:
    return any(test_id == r or test_id.startswith((r + "[", r + "::")) for r in registered)


def _id_differences(first: Mapping[str, Collection[str]], second: Mapping[str, Collection[str]] | None,
                    registered: Collection[str]) -> dict[str, Any]:
    if second is None:
        return {"compared": False, "reason": "第二側沒有獨立的測試 id 集合:沒有 Rust 版,突變 wrapper 只在 tests/parity 的突變案例中使用",
                "only_first": [], "only_second": [], "unregistered": []}
    a = {i for ids in first.values() for i in ids}
    b = {i for ids in second.values() for i in ids}
    return {"compared": True, "only_first": sorted(a - b), "only_second": sorted(b - a),
            "unregistered": sorted(i for i in a ^ b if not _is_registered(i, registered))}


def build_report(parts: Iterable[Part], sha: str, *, dirty: bool, gap_count: int,
                 second_ids: Mapping[str, Collection[str]] | None = None, registered: Collection[str] = ()) -> dict[str, Any]:
    """`registered` 是兩側差異已登記的 id(差距清單,以及 `impl_dependent.toml` 中 rust 下不適用或已替代的項目)。"""
    entries = []
    first: dict[str, set[str]] = {"pytest": set(), "behave": set()}
    for part in parts:
        outcomes = _load_outcomes(part)
        entries.append(_part_entry(part, outcomes))
        if outcomes is not None and part.kind in first:
            first[part.kind] |= set(outcomes)
    differences = _id_differences(first, second_ids, registered)
    ok = all(e["passed"] for e in entries) and bool(entries) and not differences["unregistered"]
    return {
        "format": FORMAT,
        "git_sha": sha,
        "git_dirty": dirty,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "result": "pass" if ok else "fail",
        "implementations": {
            "first": {"label": "python", "role": "受測實作:全量閘四段與 tests/parity 都跑這一側"},
            "second": {"label": "python+mutant_wrapper",
                       "role": "tests/parity/mutant_wrapper.py 包裝同一份 Python,只用來證明差分管線會抓到差異;不是第二個實作"},
        },
        "rust_side": {"exists": False, "note": "CHG-20261108-01 沒有 Rust 程式,本閘未執行任何 Rust 版"},
        "gap_count": gap_count,
        "parts": entries,
        "test_ids": {"first": {k: sorted(v) for k, v in first.items()},
                     "second": None if second_ids is None else {k: sorted(v) for k, v in second_ids.items()}},
        "id_differences": differences,
    }


def _read_parts(path: Path) -> list[Part]:
    parts = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name, kind, exit_code, seconds, results = line.split("\t")
        parts.append(Part(name, kind, int(exit_code), int(seconds), Path(results) if results else None))
    return parts


def _sentinels() -> tuple[str, ...]:
    from tests.parity.corpus import CORPUS_KEYS, SAMPLE_TOKEN

    return (*CORPUS_KEYS, SAMPLE_TOKEN)


def _print_summary(report: dict[str, Any]) -> None:
    for part in report["parts"]:
        print(f'{part["name"]}: exit={part["exit_code"]} seconds={part["seconds"]} counts={part["counts"]} '
              f'{"ok" if part["passed"] else "FAILED"}')
        for test_id in part["failed"]:
            print(f"failed:{test_id}")
        for test_id in part["skipped"]:
            print(f"skipped:{test_id}")
    for test_id in report["id_differences"]["unregistered"]:
        print(f"unregistered:{test_id}")
    print(f'local_gate: sha={report["git_sha"]} gaps={report["gap_count"]} rust_side=none result={report["result"]}')


def main(argv: list[str] | None = None) -> int:
    from tests.parity.gaps import RUST_GAPS_FILE, GapFileError, load_gaps

    ap = argparse.ArgumentParser(prog="tests.parity.gate_report")
    ap.add_argument("--sha", required=True)
    ap.add_argument("--dirty", required=True, choices=("0", "1"))
    ap.add_argument("--parts", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    try:
        parts = _read_parts(args.parts)
        gap_count = len(load_gaps(RUST_GAPS_FILE))
    except (OSError, ValueError, GapFileError) as exc:
        print(f"gate_report:{type(exc).__name__}", file=sys.stderr)
        return 2
    report = build_report(parts, args.sha, dirty=args.dirty == "1", gap_count=gap_count)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if any(s in text for s in _sentinels()):
        print("gate_report:報告含金鑰或 token 值,不寫檔", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    _print_summary(report)
    return 0 if report["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

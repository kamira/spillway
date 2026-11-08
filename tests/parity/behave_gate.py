"""behave 的差距判定(CHG-20261108-01 T2;AM-9)。

    .venv/bin/python -m behave -f json -o report.json tests/features
    .venv/bin/python -m tests.parity.behave_gate report.json [--root tests/features]

behave 沒有 xfail,feature 文字也不寫(Global Constraints),所以由本程式讀 `-f json` 輸出對照 `rust_gaps.toml`,決定結束碼。
`SPILLWAY_IMPL=python`(或未設)時不讀清單,任何失敗都是失敗。

判定(結束碼 0 全部符合、1 有不符、2 輸入或設定不合法):
- 清單內:必須失敗。可接受的失敗只有 behave 狀態 `failed`(步驟丟 AssertionError,含其子類別 harness 的
  `EndpointNotImplemented`)。狀態 `error`(其他例外,含 serve 起不來的 RuntimeError;behave 1.3 的 json 不含其例外內容)、
  hook 失敗、undefined、skipped、通過(strict)都判不符;訊息含真實 CLI 護欄固定文字(`gaps.REAL_CLI_TEXT`)的 `failed` 也判不符;
  `expect` 必須出現在失敗文字中。
- 清單外:`failed`、`error`、`hook_error`、`undefined`、`pending` 等都判不符;`skipped` 以 `skipped:<id>` 逐項列在 stdout,
  不判失敗(AM-11),但全部情境都是 skipped 時結束碼 2。
- feature 層 hook 失敗(`hook_error`、`cleanup_error`)判不符,Python 與 Rust 都適用。`after_all` 不在 json 內,本程式看不到。
- 報告必須涵蓋 `behave --dry-run` 收集到的每個情境,清單中位於 `--root` 下的 id 也必須出現(behave 中途崩潰不能算過);
  同一 feature 內重名的情境是結束碼 2(後者會蓋掉前者的結果)。
- rust 下讀 `impl_dependent.toml` 的 behave 項目:`not_applicable`,或 `replace` 且替代情境在報告中者,從結果與預期情境一併移除並在
  摘要列計數(T2 仲裁 A2,依決策 5);被移除的 id 同時列在差距清單時結束碼 2。

輸出只有情境 id 與狀態,不轉印錯誤內容(可能含秘密)。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from tests.blackbox.harness import ImplConfigError, spillway_impl
from tests.parity import gaps as gaps_mod
from tests.parity.gaps import Gap, GapFileError, ImplDependent


class ReportError(ValueError):
    """報告檔不可用。"""


@dataclass(frozen=True)
class Outcome:
    status: str
    message: str = ""


def _step_message(result: dict) -> str:
    msg = result.get("error_message", "")
    return "\n".join(msg) if isinstance(msg, list) else str(msg)


def scenario_outcomes(report: list) -> dict[str, Outcome]:
    out: dict[str, Outcome] = {}
    for feat in report:
        path = feat["location"].rsplit(":", 1)[0]
        for el in feat.get("elements", []):
            if el.get("type") != "scenario":
                continue
            message = next((_step_message(s["result"]) for s in el.get("steps", [])
                            if s.get("result", {}).get("status") == "failed"), "")
            key = f'{path}::{el["name"]}'
            if key in out:
                raise ReportError(f"重名的情境:{key}")
            out[key] = Outcome(str(el.get("status")), message)
    return out


def feature_problems(report: list) -> list[str]:
    return [f'feature hook 失敗:{feat["location"].rsplit(":", 1)[0]}({feat["status"]})'
            for feat in report if feat.get("status") in ("hook_error", "cleanup_error")]


def evaluate(outcomes: dict[str, Outcome], gap_list: Iterable[Gap], expected: set[str]) -> list[str]:
    """回傳不符項目(只含 id 與狀態)。"""
    problems = [f"報告缺少情境:{i}" for i in sorted(expected - set(outcomes))]
    by_id = {g.id: g for g in gap_list}
    for gid in sorted(set(by_id) - set(outcomes)):
        problems.append(f"差距清單的情境不在報告中:{gid}")
    for sid, o in sorted(outcomes.items()):
        gap = by_id.get(sid)
        if gap is None:
            if o.status not in ("passed", "skipped"):
                problems.append(f"失敗:{sid}({o.status})")
        elif o.status == "passed":
            problems.append(f"差距項目通過,必須從清單刪除(strict):{sid}")
        elif o.status != "failed":
            problems.append(f"差距項目的狀態不是可接受的預期失敗:{sid}({o.status})")
        elif any(t in o.message for t in gaps_mod.REAL_CLI_TEXT):
            problems.append(f"差距項目命中真實 CLI 護欄,不是預期失敗:{sid}")
        elif gap.expect is not None and gap.expect not in o.message:
            problems.append(f"差距項目的失敗特徵不符:{sid}")
    return problems


def expected_ids(roots: list[str]) -> set[str]:
    return gaps_mod.collect_behave_ids(roots, Path.cwd())


def _load_report(path: Path) -> list:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReportError(f"報告檔無法讀取:{path}({type(exc).__name__})") from exc
    if not isinstance(report, list) or not report:
        raise ReportError(f"報告檔沒有任何 feature:{path}")
    return report


def rust_deselected(entries: Iterable[ImplDependent], outcomes: dict[str, Outcome]) -> tuple[set[str], set[str]]:
    """回傳 (not_applicable 的 id, replace 且替代情境在報告中的 id)。"""
    behave = [e for e in entries if e.kind == "behave"]
    return ({e.id for e in behave if e.action == "not_applicable"},
            {e.id for e in behave if e.action == "replace" and e.target in outcomes})


def main(argv: list[str] | None = None, *, gaps_file: Path | None = None, impl_file: Path | None = None) -> int:
    """`gaps_file`、`impl_file` 只給內部測試使用,命令列沒有對應選項。"""
    ap = argparse.ArgumentParser(prog="tests.parity.behave_gate")
    ap.add_argument("report", type=Path)
    ap.add_argument("--root", action="append", default=None, help="behave 的 feature 根目錄,預設 tests/features")
    args = ap.parse_args(argv)
    roots = [r.rstrip("/") for r in (args.root or ["tests/features"])]
    try:
        impl = spillway_impl()
        report = _load_report(args.report)
        outcomes = scenario_outcomes(report)
        hook_problems = feature_problems(report)
        gap_list: list[Gap] = []
        not_applicable: set[str] = set()
        replaced: set[str] = set()
        if impl == "rust":
            gap_list = [g for g in gaps_mod.load_gaps(gaps_file or gaps_mod.RUST_GAPS_FILE)
                        if g.kind == "behave" and any(g.id.startswith(r + "/") for r in roots)]
            not_applicable, replaced = rust_deselected(
                gaps_mod.load_impl_dependent(impl_file or gaps_mod.IMPL_DEPENDENT_FILE), outcomes)
            overlap = sorted((not_applicable | replaced) & {g.id for g in gap_list})
            if overlap:
                raise GapFileError(f"同一個 id 不得同時在差距清單與 deselect 清單:{overlap}")
        expected = expected_ids(roots) - not_applicable - replaced
    except (ImplConfigError, ReportError, GapFileError, KeyError, TypeError, AttributeError) as exc:
        print(f"behave_gate:{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    dropped_na = sum(1 for i in not_applicable if outcomes.pop(i, None) is not None)
    dropped_replaced = sum(1 for i in replaced if outcomes.pop(i, None) is not None)
    if not expected or not outcomes:
        print("behave_gate:沒有任何情境", file=sys.stderr)
        return 2
    for sid, o in sorted(outcomes.items()):
        if o.status == "skipped":
            print(f"skipped:{sid}")
    if all(o.status == "skipped" for o in outcomes.values()):
        print("behave_gate:所有情境都是 skipped", file=sys.stderr)
        return 2
    problems = [*hook_problems, *evaluate(outcomes, gap_list, expected)]
    for line in problems:
        print(line, file=sys.stderr)
    counts = {s: sum(1 for o in outcomes.values() if o.status == s) for s in sorted({o.status for o in outcomes.values()})}
    print(f"behave_gate: impl={impl} scenarios={len(outcomes)} gaps={len(gap_list)} "
          f"not_applicable deselected={dropped_na} replaced deselected={dropped_replaced} statuses={counts} "
          f"result={'ok' if not problems else 'mismatch'}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""黑箱化前的基準清單(CHG-20261107-03 AM-10;T2)。

以 `git worktree add <scratch> 7fadd84 --detach` 取出轉換前的工作樹後執行:

    PYTHONPATH=<wt>/src:<wt> LITELLM_LOCAL_MODEL_COST_MAP=True \\
        .venv/bin/python artifacts/m5a/baseline/generate.py <wt> artifacts/m5a/baseline

產出(皆為 UTF-8 JSON,鍵已排序):
- `pytest_nodeids.json`:`pytest --collect-only -q tests/acceptance tests/ui` 的 node ID 全集,依目錄分組。
- `behave_scenarios.json`:`behave --dry-run -f json` 的情境清單(feature、名稱、位置、步驟數(含 background)、
  每步的關鍵字與文字)。
- `assert_counts.json`:每個函式的 `assert` 敘述數與 `expect(` 呼叫數(AST;不含巢狀函式,巢狀函式各自一列)。
  - `functions`:非步驟函式,鍵為 `相對路徑::qualname`(同名重複時加 `#n`)。
  - `steps`:behave 步驟函式,鍵為步驟的 pattern(behave 內全域唯一),值另含所在檔案與函式名。
  掃描範圍:`tests/acceptance`、`tests/ui`、`tests/features`、`tests/helpers/ui*.py`(後者由 T2 複製到
  `tests/blackbox/ui/`)。
- `summary.json`:各項數量與來源 commit。

驗收時以同一份 AST 規則掃描轉換後的檔案對照(node ID 與情境集合相同;每個函式的斷言數不低於基準)。
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

STEP_DECORATORS = {"step", "given", "when", "then", "Given", "When", "Then", "Step"}


def _dump(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _env(wt: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(wt / "src"), str(wt)])
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    return env


def collect_nodeids(wt: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for d in ("tests/acceptance", "tests/ui"):
        r = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", d],
                           cwd=wt, env=_env(wt), capture_output=True, text=True, check=True)
        out[d] = sorted(ln for ln in r.stdout.splitlines() if "::" in ln)
    return out


def collect_scenarios(wt: Path) -> list[dict[str, object]]:
    with tempfile.TemporaryDirectory() as tmp:
        js = Path(tmp) / "dry.json"
        subprocess.run([sys.executable, "-m", "behave", "--dry-run", "-f", "json", "-o", str(js), "tests/features"],
                       cwd=wt, env=_env(wt), capture_output=True, text=True, check=True)
        data = json.loads(js.read_text(encoding="utf-8"))
    out = []
    for feat in data:
        for el in feat.get("elements", []):
            if el["type"] != "scenario":
                continue
            out.append({
                "feature": feat["location"].rsplit(":", 1)[0],
                "name": el["name"],
                "location": el["location"],
                "step_count": len(el["steps"]),
                "steps": [f'{s["keyword"]} {s["name"]}' for s in el["steps"]],
            })
    return out


def _decorator_patterns(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    pats = []
    for dec in fn.decorator_list:
        if isinstance(dec, ast.Call):
            f = dec.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name in STEP_DECORATORS and dec.args and isinstance(dec.args[0], ast.Constant):
                pats.append(str(dec.args[0].value))
    return pats


def _direct_counts(fn: ast.AST) -> tuple[int, int]:
    """函式本體內(不進入巢狀函式/類別)的 assert 數與 expect( 呼叫數。"""
    asserts = expects = 0
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, ast.Assert):
            asserts += 1
        elif isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Name) and f.id == "expect") or (isinstance(f, ast.Attribute) and f.attr == "expect"):
                expects += 1
        stack.extend(ast.iter_child_nodes(node))
    return asserts, expects


def count_asserts(wt: Path) -> dict[str, dict[str, object]]:
    files: list[Path] = []
    for d in ("tests/acceptance", "tests/ui", "tests/features"):
        files += sorted((wt / d).rglob("*.py"))
    files += sorted((wt / "tests/helpers").glob("ui*.py"))
    functions: dict[str, object] = {}
    steps: dict[str, object] = {}
    for path in files:
        rel = path.relative_to(wt).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)

        def walk(node: ast.AST, prefix: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    walk(child, f"{prefix}{child.name}.")
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qual = f"{prefix}{child.name}"
                    a, e = _direct_counts(child)
                    pats = _decorator_patterns(child)
                    if pats:
                        for p in pats:
                            assert p not in steps, f"步驟 pattern 重複:{p}"
                            steps[p] = {"file": rel, "function": qual, "asserts": a, "expects": e}
                    else:
                        key = f"{rel}::{qual}"
                        n = 2
                        while key in functions:
                            key = f"{rel}::{qual}#{n}"
                            n += 1
                        functions[key] = {"asserts": a, "expects": e}
                    walk(child, f"{qual}.<locals>.")

        walk(tree, "")
    return {"functions": functions, "steps": steps}


def main() -> int:
    wt, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True,
                            check=True).stdout.strip()
    nodeids = collect_nodeids(wt)
    scenarios = collect_scenarios(wt)
    counts = count_asserts(wt)
    _dump(out / "pytest_nodeids.json", nodeids)
    _dump(out / "behave_scenarios.json", scenarios)
    _dump(out / "assert_counts.json", counts)
    fn, st = counts["functions"], counts["steps"]
    _dump(out / "summary.json", {
        "commit": commit,
        "pytest_nodeids": {d: len(v) for d, v in nodeids.items()},
        "behave_features": len({s["feature"] for s in scenarios}),
        "behave_scenarios": len(scenarios),
        "behave_steps": sum(int(s["step_count"]) for s in scenarios),
        "functions": len(fn), "step_functions": len(st),
        "asserts_total": sum(v["asserts"] for v in fn.values()) + sum(v["asserts"] for v in st.values()),
        "expects_total": sum(v["expects"] for v in fn.values()) + sum(v["expects"] for v in st.values()),
    })
    print(json.dumps(json.loads((out / "summary.json").read_text(encoding="utf-8")), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

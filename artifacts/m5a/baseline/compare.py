"""黑箱化前後的基準對照(CHG-20261107-03 AM-10;施工審議 T2 F13)。

    .venv/bin/python artifacts/m5a/baseline/compare.py [--repo .] [--roots tests/acceptance ...]
        [--behave-json FILE] [--diff-base 7fadd84 | --no-diff]

`--roots` 省略時為全集(`tests/acceptance`、`tests/ui`、`tests/features`)。T3、T4、T5 收尾時各以
`--roots <自己的目錄>` 執行並附上輸出;全部轉換完成後再對全集執行一次。`tests/features` 在範圍內時必須給
`--behave-json`(`behave -f json -o FILE tests/features` 的實際執行結果)。

檢查(任一項不符即列出並以非 0 結束):
1. 集合:範圍內的 pytest node ID(`pytest --collect-only -q`)與 behave 情境(dry-run json 的 feature + 名稱)
   與基準相同;每個情境的步驟數與基準相同。
2. 斷言數:基準中範圍內每個函式與步驟函式的 `assert` 數、`expect(` 數不低於基準(與 `generate.py` 同一套
   AST 規則)。函式鍵以 `FILE_MOVES`(`tests/helpers/ui*.py` → `tests/blackbox/ui/`、
   `tests/features/support/mcp_proc.py` → `tests/blackbox/mcp.py`)、`FUNCTION_MOVES`(`ServeProc.*` →
   `harness.Serve`、`child_env` → `harness.child_env`、`redact` → `mask.redact_tokens`)對應到新位置(寫死,不做模糊比對);`REMOVED` 列出有意
   移除的函式與理由;其餘比對不到的函式一律算錯誤。`tests/helpers/ui*.py` 的函式在 `tests/ui` 屬於範圍時檢查
   (T2 已複製到 `tests/blackbox/ui/`)。
3. diff:`git diff <diff-base> -- <roots>` 的新增行不得含 `skip`、`skipif`、`xfail`、`except AssertionError`;
   範圍內的 behave 步驟函式本體不得只有 `pass`(或 docstring)。`--no-diff` 略過 git diff(例如對基準 commit
   本身的快照執行時)。
4. behave 結果(`--behave-json`):0 skipped、0 undefined(並列出 failed 數)。

需要新的對應(函式搬家、步驟 pattern 改名)時回報主 session,由 T2 擁有者補進本檔的對照表。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate import _decorator_patterns, _direct_counts  # noqa: E402

BASELINE_DIR = Path(__file__).resolve().parent
ALL_ROOTS = ("tests/acceptance", "tests/ui", "tests/features")
PYTEST_ROOTS = ("tests/acceptance", "tests/ui")

# T2 把 tests/helpers/ui*.py 複製改寫到 tests/blackbox/ui/(函式名不變);舊檔由 T5 刪除
FILE_MOVES = {
    "tests/helpers/ui.py": "tests/blackbox/ui/__init__.py",
    "tests/helpers/ui_ledger.py": "tests/blackbox/ui/ledger.py",
    "tests/helpers/ui_mcp.py": "tests/blackbox/ui/mcp.py",
    "tests/helpers/ui_members.py": "tests/blackbox/ui/members.py",
    "tests/helpers/ui_overview.py": "tests/blackbox/ui/overview.py",
    "tests/helpers/ui_routes.py": "tests/blackbox/ui/routes.py",
    "tests/helpers/ui_settings.py": "tests/blackbox/ui/settings.py",
    # T2 把 MCP 驅動複製改寫到 tests/blackbox/mcp.py(LoopThread、HeldSession、mcp_http_session、raw_get_stream、
    # stdio_session、ask_route_enum、pid_alive、group_alive、is_list_changed);T4 刪除原檔
    "tests/features/support/mcp_proc.py": "tests/blackbox/mcp.py",
}
# 個別函式的搬家:基準鍵 → 新鍵(`檔案::qualname`)
_MP = "tests/features/support/mcp_proc.py::"
FUNCTION_MOVES: dict[str, str] = {
    # 子行程 serve 改由 harness.Serve 負責(決策 8 ②)
    _MP + "ServeProc.__init__": "tests/blackbox/harness.py::Serve.__init__",
    _MP + "ServeProc.wait_ready": "tests/blackbox/harness.py::Serve.wait_ready",
    _MP + "ServeProc.wait_exit": "tests/blackbox/harness.py::Serve.wait_exit",
    _MP + "ServeProc.log_text": "tests/blackbox/harness.py::Serve.stderr_bytes",
    _MP + "ServeProc.safe_log": "tests/blackbox/harness.py::Serve.stderr_text",   # 遮 token 的 stderr
    _MP + "ServeProc.stop": "tests/blackbox/harness.py::Serve.stop",
    _MP + "child_env": "tests/blackbox/harness.py::child_env",
    _MP + "redact": "tests/blackbox/mask.py::redact_tokens",
}
# 步驟 pattern 的改名:基準 pattern → 新 pattern(feature 文字不改,原則上不需要)
STEP_RENAMES: dict[str, str] = {}
# 有意移除的函式 → 理由
REMOVED = {
    "tests/helpers/ui.py::db_execute": "任意 SQL 寫入;黑箱版改經 /__test__/*(CHG-20261107-03 決策 3;T2)",
}
_DIFF_BANNED = re.compile(r"(?<![\w])skip(if)?(?![\w])|xfail|except\s+AssertionError")


def _env(repo: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(repo / "src"), str(repo)])
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    return env


def _under(rel: str, roots: list[str]) -> bool:
    return any(rel == r or rel.startswith(r + "/") for r in roots)


# ---------------------------------------------------------------------------- 1. 集合


def nodeids(repo: Path, root: str) -> set[str]:
    r = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", root],
                       cwd=repo, env=_env(repo), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"pytest --collect-only {root} 失敗:\n{r.stdout[-2000:]}{r.stderr[-2000:]}")
    return {ln for ln in r.stdout.splitlines() if "::" in ln}


def scenarios(repo: Path) -> dict[tuple[str, str], int]:
    with tempfile.TemporaryDirectory() as tmp:
        js = Path(tmp) / "dry.json"
        subprocess.run([sys.executable, "-m", "behave", "--dry-run", "-f", "json", "-o", str(js), "tests/features"],
                       cwd=repo, env=_env(repo), capture_output=True, text=True, check=True)
        data = json.loads(js.read_text(encoding="utf-8"))
    return {(f["location"].rsplit(":", 1)[0], e["name"]): len(e["steps"])
            for f in data for e in f.get("elements", []) if e["type"] == "scenario"}


def check_sets(repo: Path, roots: list[str], errors: list[str]) -> None:
    base_ids = json.loads((BASELINE_DIR / "pytest_nodeids.json").read_text(encoding="utf-8"))
    for root in roots:
        if root in PYTEST_ROOTS:
            want, got = set(base_ids[root]), nodeids(repo, root)
            for x in sorted(want - got):
                errors.append(f"[nodeid] 基準有、現在沒有:{x}")
            for x in sorted(got - want):
                errors.append(f"[nodeid] 現在多出:{x}")
    if "tests/features" in roots:
        base = {(s["feature"], s["name"]): s["step_count"]
                for s in json.loads((BASELINE_DIR / "behave_scenarios.json").read_text(encoding="utf-8"))}
        got = scenarios(repo)
        for k in sorted(set(base) - set(got)):
            errors.append(f"[scenario] 基準有、現在沒有:{k[0]}::{k[1]}")
        for k in sorted(set(got) - set(base)):
            errors.append(f"[scenario] 現在多出:{k[0]}::{k[1]}")
        for k in sorted(set(base) & set(got)):
            if base[k] != got[k]:
                errors.append(f"[steps] {k[0]}::{k[1]} 步驟數 {got[k]},基準 {base[k]}")


# ---------------------------------------------------------------------------- 2. 斷言數


def scan(repo: Path, files: list[Path]) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, object]]]:
    functions: dict[str, dict[str, int]] = {}
    steps: dict[str, dict[str, object]] = {}
    for path in files:
        rel = path.relative_to(repo).as_posix()
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
                        only_pass = all(isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(
                            s.value, ast.Constant)) for s in child.body)
                        for p in pats:
                            steps[p] = {"file": rel, "function": qual, "asserts": a, "expects": e,
                                        "empty": only_pass}
                    else:
                        key = f"{rel}::{qual}"
                        n = 2
                        while key in functions:
                            key = f"{rel}::{qual}#{n}"
                            n += 1
                        functions[key] = {"asserts": a, "expects": e}
                    walk(child, f"{qual}.<locals>.")

        walk(tree, "")
    return functions, steps


def _candidates(key: str) -> list[str]:
    """新位置優先;找不到時用原鍵(對基準 commit 本身或尚未刪除舊檔的樹執行時)。"""
    out = [FUNCTION_MOVES[key]] if key in FUNCTION_MOVES else []
    file, _, rest = key.partition("::")
    if file in FILE_MOVES:
        out.append(f"{FILE_MOVES[file]}::{rest}")
    out.append(key)
    return out


def check_asserts(repo: Path, roots: list[str], errors: list[str], report: list[str]) -> None:
    base = json.loads((BASELINE_DIR / "assert_counts.json").read_text(encoding="utf-8"))
    files: list[Path] = []
    for d in (*ALL_ROOTS, "tests/blackbox/ui"):
        if (repo / d).is_dir():
            files += sorted(p for p in (repo / d).rglob("*.py") if "__pycache__" not in p.parts)
    files += sorted((repo / "tests/blackbox").glob("*.py"))   # harness、mcp、mask(搬移目標)
    files += sorted((repo / "tests/helpers").glob("ui*.py"))
    functions, steps = scan(repo, files)
    helpers_in_scope = "tests/ui" in roots
    checked = 0
    for key, want in sorted(base["functions"].items()):
        file = key.partition("::")[0]
        in_scope = _under(file, roots) or (helpers_in_scope and file in FILE_MOVES)
        if not in_scope:
            continue
        if key in REMOVED:
            report.append(f"[removed] {key}:{REMOVED[key]}")
            continue
        got_key = next((c for c in _candidates(key) if c in functions), None)
        if got_key is None:
            errors.append(f"[function] 比對不到:{key}(新位置請補進 compare.py 的 FUNCTION_MOVES 或 REMOVED)")
            continue
        checked += 1
        got = functions[got_key]
        for kind in ("asserts", "expects"):
            if got[kind] < want[kind]:
                errors.append(f"[{kind}] {key} → {got_key}:{got[kind]} < 基準 {want[kind]}")
    if "tests/features" in roots:
        for pat, want in sorted(base["steps"].items()):
            new = STEP_RENAMES.get(pat, pat)
            got = steps.get(new)
            if got is None:
                errors.append(f"[step] 比對不到步驟 pattern:{pat}")
                continue
            checked += 1
            for kind in ("asserts", "expects"):
                if int(got[kind]) < int(want[kind]):
                    errors.append(f"[{kind}] 步驟 {pat}({got['file']}::{got['function']}):"
                                  f"{got[kind]} < 基準 {want[kind]}")
            if got["empty"]:
                errors.append(f"[empty-step] 步驟本體只有 pass:{pat}")
    report.append(f"[asserts] 對照 {checked} 個函式與步驟")


# ---------------------------------------------------------------------------- 3. diff、4. behave 結果


def banned_added_lines(diff_text: str) -> list[str]:
    """unified diff 中新增行(`+` 開頭、不含 `+++` 檔頭)含禁用字樣者。"""
    return [ln for ln in diff_text.splitlines()
            if ln.startswith("+") and not ln.startswith("+++") and _DIFF_BANNED.search(ln[1:])]


def check_diff(repo: Path, roots: list[str], base: str, errors: list[str]) -> None:
    r = subprocess.run(["git", "diff", base, "--", *roots], cwd=repo, capture_output=True, text=True, check=True)
    for ln in banned_added_lines(r.stdout):
        errors.append(f"[diff] 新增行含 skip/xfail/except AssertionError:{ln[:200]}")


def check_behave_json(path: Path, errors: list[str], report: list[str]) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for f in data:
        for e in f.get("elements", []):
            if e["type"] != "scenario":
                continue
            counts[e.get("status", "?")] = counts.get(e.get("status", "?"), 0) + 1
            for s in e["steps"]:
                st = (s.get("result") or {}).get("status", "untested")
                key = f"step:{st}"
                counts[key] = counts.get(key, 0) + 1
    report.append(f"[behave] {json.dumps(counts, sort_keys=True)}")
    for bad in ("skipped", "undefined", "step:skipped", "step:undefined", "untested", "step:untested"):
        if counts.get(bad):
            errors.append(f"[behave] {bad} = {counts[bad]}")
    if counts.get("failed"):
        errors.append(f"[behave] failed = {counts['failed']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--roots", nargs="+", choices=ALL_ROOTS, default=list(ALL_ROOTS))
    ap.add_argument("--behave-json")
    ap.add_argument("--diff-base", default="7fadd84")
    ap.add_argument("--no-diff", action="store_true")
    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve()
    roots = list(dict.fromkeys(args.roots))
    errors: list[str] = []
    report: list[str] = [f"[roots] {' '.join(roots)}"]
    if "tests/features" in roots and not args.behave_json:
        ap.error("tests/features 在範圍內時必須給 --behave-json")
    check_sets(repo, roots, errors)
    check_asserts(repo, roots, errors, report)
    if not args.no_diff:
        check_diff(repo, roots, args.diff_base, errors)
    if args.behave_json:
        check_behave_json(Path(args.behave_json), errors, report)
    for ln in report:
        print(ln)
    for ln in errors:
        print("ERROR " + ln)
    print(f"結果:{'通過' if not errors else f'{len(errors)} 項不符'}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Rust 差距清單的讀取、驗證與對照(CHG-20261108-01 T2;AM-9;決策 5)。不 import spillway。

檔案
- `rust_gaps.toml`:`[[gap]]` 含 `kind`(pytest/behave)、`id`、`reason`、`batch`(移植批次)、`since`(CHG);選填
  `expect`(預期失敗特徵,失敗文字必須包含此子字串)、`readd_chg`(見單向規則)。
- `protected.toml`:`[[protected]]` 的 `file` + `names`(`*` 是唯一的萬用字元;參數化測試以名稱比對全部變體)。
  修改需走安全邊界停點。
- `tests/blackbox/impl_dependent.toml`:`[[entry]]`(單一測試)與 `[[file_entry]]`(整個檔案);`on_rust` 為
  `replace:<測試 id>`、`not_applicable`(必須有 `on_rust_reason`)或 `gap`。

規則
- xfail 一律 strict,`raises` 只有 `XFAIL_RAISES`(serve 起不來是 RuntimeError 或 OSError,不算 xfail)。
- 受保護清單與差距清單交集為空(`protected_hits`)。
- 單向(`main_deleted_ids`、`one_way_violations`):`main` 歷史上出現過、目前 `main` 已沒有的 id 不得加回;例外是該項
  `readd_chg` 指向的 CHG 文件逐字列出此 id(決策 6 判定)。`main` 上沒有此檔案視為空清單;沒有 `main` ref 則失敗。
  「新增項只限新測試」無法從檔案判定(下一筆跑 Rust 閘的 CHG 可一次列入既有測試),由審查把關。
- id 一律以實際收集結果比對(`collect_pytest_ids`、`collect_behave_ids`),不用凍結基準。

產生清單:`python -m tests.parity.gaps render --kind pytest --ids-file F --reason R --batch B --since CHG-…`,
先驗證 id 存在於實際收集結果,再輸出 `[[gap]]`。

本模組也是 pytest plugin(根 `tests/conftest.py` 的 `pytest_plugins`):`pytest_configure` 解析 `SPILLWAY_IMPL`,不合法是 usage
error。只有 rust 時才讀清單(python 套用 0 項):清單內的 pytest 測試加 `xfail(strict=True, raises=XFAIL_RAISES)`,`expect` 不符或
測試被 skip 都判失敗,受保護的 id 進清單時整個 run 是 usage error;`impl_dependent.toml` 的 `not_applicable` 項目 deselect 並在
結尾計數,`replace`、`gap` 不做任何事(照常執行、照常失敗)。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Collection, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.blackbox.guard import REPO_ROOT, converted_roots
from tests.blackbox.harness import EndpointNotImplemented, ImplConfigError, spillway_impl
from tests.blackbox.plugin import _FAILED_KEY

__all__ = ["IMPL_DEPENDENT_FILE", "KINDS", "PROTECTED_FILE", "RUST_GAPS_FILE", "RUST_GAPS_RELPATH", "XFAIL_RAISES",
           "Gap", "GapFileError", "ImplDependent", "Protected", "collect_behave_ids", "collect_pytest_ids",
           "expand_protected", "id_matches", "load_gaps", "load_impl_dependent", "load_protected", "main_deleted_ids",
           "missing_replacements", "one_way_violations", "parse_on_rust", "protected_hits", "render_gaps",
           "scenario_ids"]

RUST_GAPS_RELPATH = "tests/parity/rust_gaps.toml"
RUST_GAPS_FILE = REPO_ROOT / RUST_GAPS_RELPATH
PROTECTED_FILE = REPO_ROOT / "tests/parity/protected.toml"
IMPL_DEPENDENT_FILE = REPO_ROOT / "tests/blackbox/impl_dependent.toml"
XFAIL_RAISES: tuple[type[BaseException], ...] = (AssertionError, EndpointNotImplemented)
KINDS = ("pytest", "behave")
BEHAVE_ROOT_CANDIDATES = ("tests/features", "tests/parity/features")

_CHG_RE = re.compile(r"CHG-\d{8}-\d{2}")
_GAP_REQUIRED = ("kind", "id", "reason", "batch", "since")
_GAP_OPTIONAL = ("expect", "readd_chg")
_IMPL_KEYS = {"kind", "id", "reason", "scope", "on_rust", "on_rust_reason"}
_FILE_KEYS = {"id", "reason", "on_rust", "on_rust_reason"}
_COLLECT_DROP_ENV = ("SPILLWAY_CMD", "SPILLWAY_IMPL")


class GapFileError(ValueError):
    """清單檔案不合法(或無法判定單向規則)。"""


@dataclass(frozen=True)
class Gap:
    kind: str
    id: str
    reason: str
    batch: str
    since: str
    expect: str | None = None
    readd_chg: str | None = None


@dataclass(frozen=True)
class Protected:
    file: str
    name: str

    @property
    def pattern(self) -> str:
        return f"{self.file}::{self.name}"


@dataclass(frozen=True)
class ImplDependent:
    kind: str                  # pytest、behave;整個檔案的項目為 "file"
    id: str
    action: str                # replace、not_applicable、gap
    target: str | None = None  # replace 的替代測試 id

    @property
    def is_file(self) -> bool:
        return self.kind == "file"

    def covers(self, nodeid: str) -> bool:
        return nodeid.startswith(self.id + "::") if self.is_file else id_matches(self.id, nodeid)


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise GapFileError(f"{path}: {exc}") from exc


def _table_list(data: dict, key: str, allowed: set[str], source: object) -> list[dict]:
    unknown = set(data) - allowed
    if unknown:
        raise GapFileError(f"{source}: 未知的頂層鍵 {sorted(unknown)}")
    rows = data.get(key, [])
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise GapFileError(f"{source}: {key} 必須是 [[{key}]] 表格陣列")
    return rows


def _text(row: dict, key: str, source: object) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise GapFileError(f"{source}: {key} 必須是非空字串:{row}")
    return value


def parse_gaps(data: dict, source: object) -> list[Gap]:
    out: list[Gap] = []
    seen: set[str] = set()
    for row in _table_list(data, "gap", {"gap"}, source):
        unknown = set(row) - set(_GAP_REQUIRED) - set(_GAP_OPTIONAL)
        if unknown:
            raise GapFileError(f"{source}: 未知欄位 {sorted(unknown)}:{row}")
        fields = {k: _text(row, k, source) for k in _GAP_REQUIRED}
        for k in _GAP_OPTIONAL:
            if k in row:
                fields[k] = _text(row, k, source)
        gap = Gap(**fields)
        if gap.kind not in KINDS:
            raise GapFileError(f"{source}: kind 必須是 {KINDS}:{gap.id}")
        for chg in (gap.since, gap.readd_chg):
            if chg is not None and not _CHG_RE.fullmatch(chg):
                raise GapFileError(f"{source}: 不是 CHG 編號:{chg}")
        if gap.id in seen:
            raise GapFileError(f"{source}: id 重複:{gap.id}")
        seen.add(gap.id)
        out.append(gap)
    return out


def load_gaps(path: Path) -> list[Gap]:
    return parse_gaps(_read_toml(path), path)


def load_protected(path: Path) -> list[Protected]:
    out = []
    for row in _table_list(_read_toml(path), "protected", {"protected"}, path):
        names = row.get("names")
        if set(row) != {"file", "names"} or not isinstance(names, list) or not names:
            raise GapFileError(f"{path}: [[protected]] 必須且只能有 file、names:{row}")
        out.extend(Protected(_text(row, "file", path), _text({"n": n}, "n", path)) for n in names)
    return out


def expand_protected(entries: Iterable[Protected], collected: Collection[str]) -> tuple[dict[str, set[str]], list[str]]:
    """回傳 (pattern → 展開到的 node ID, 沒有展開到任何 node ID 的 pattern)。"""
    expanded: dict[str, set[str]] = {}
    for e in entries:
        body = ".*".join(re.escape(part) for part in e.pattern.split("*"))
        rx = re.compile(body + r"(\[.*\])?")
        expanded[e.pattern] = {i for i in collected if rx.fullmatch(i)}
    return expanded, sorted(p for p, ids in expanded.items() if not ids)


def protected_hits(gap_list: Iterable[Gap], protected_ids: Collection[str]) -> list[str]:
    return sorted(g.id for g in gap_list if g.id in protected_ids)


def id_matches(entry_id: str, nodeid: str) -> bool:
    """`impl_dependent.toml` 的 id 不含參數,涵蓋該測試的全部參數化變體。"""
    return nodeid == entry_id or nodeid.startswith(entry_id + "[")


def parse_on_rust(value: object) -> tuple[str, str | None]:
    if value in ("gap", "not_applicable"):
        return str(value), None
    if isinstance(value, str) and value.startswith("replace:"):
        target = value.removeprefix("replace:")
        if "::" in target and all(target.split("::", 1)):
            return "replace", target
    raise GapFileError(f"on_rust 必須是 replace:<測試 id>、not_applicable 或 gap:{value!r}")


def _impl_row(row: dict, kind: str, allowed: set[str], path: Path) -> ImplDependent:
    unknown = set(row) - allowed
    if unknown:
        raise GapFileError(f"{path}: 未知欄位 {sorted(unknown)}:{row}")
    entry_id = _text(row, "id", path)
    _text(row, "reason", path)
    if "on_rust" not in row:
        raise GapFileError(f"{path}: 缺 on_rust:{entry_id}")
    action, target = parse_on_rust(row["on_rust"])
    if action == "not_applicable":
        _text(row, "on_rust_reason", path)
    return ImplDependent(kind, entry_id, action, target)


def load_impl_dependent(path: Path) -> list[ImplDependent]:
    data = _read_toml(path)
    allowed = {"entry", "file_entry"}
    out = []
    for row in _table_list(data, "entry", allowed, path):
        if row.get("kind") not in KINDS:
            raise GapFileError(f"{path}: kind 必須是 {KINDS}:{row}")
        out.append(_impl_row(row, row["kind"], _IMPL_KEYS, path))
    for row in _table_list(data, "file_entry", allowed, path):
        out.append(_impl_row(row, "file", _FILE_KEYS, path))
    return out


def missing_replacements(entries: Iterable[ImplDependent], collected: Collection[str]) -> list[ImplDependent]:
    """`on_rust = replace` 但替代測試還不存在的項目;決策 5「追齊」要求這份清單為空。"""
    return [e for e in entries if e.action == "replace" and e.target not in collected]


# ---------------------------------------------------------------- 單向規則


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True)


def _ids_at(repo: Path, rev: str) -> set[str]:
    shown = _git(repo, "show", f"{rev}:{RUST_GAPS_RELPATH}")
    if shown.returncode != 0:     # 該版本沒有此檔案:視為空清單
        return set()
    try:
        data = tomllib.loads(shown.stdout)
    except tomllib.TOMLDecodeError as exc:
        raise GapFileError(f"{rev}:{RUST_GAPS_RELPATH}: {exc}") from exc
    return {g.id for g in parse_gaps(data, f"{rev}:{RUST_GAPS_RELPATH}")}


def main_deleted_ids(repo: Path, ref: str = "main") -> set[str]:
    """`ref` 歷史上出現過、目前 `ref` 已沒有的 id。"""
    if _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode != 0:
        raise GapFileError(f"找不到 {ref} ref,無法判定單向規則")
    log = _git(repo, "log", "--full-history", "--format=%H", ref, "--", RUST_GAPS_RELPATH)
    if log.returncode != 0:
        raise GapFileError(f"git log 失敗:{log.stderr.strip()}")
    seen: set[str] = set()
    for sha in log.stdout.split():
        seen |= _ids_at(repo, sha)
    return seen - _ids_at(repo, ref)


def one_way_violations(current: Iterable[Gap], deleted: Collection[str], repo: Path) -> list[str]:
    bad = []
    for g in current:
        if g.id not in deleted:
            continue
        doc = repo / "sdlc_docs/changes" / f"{g.readd_chg}.md"
        if g.readd_chg is None or not doc.is_file() or g.id not in doc.read_text(encoding="utf-8"):
            bad.append(g.id)
    return sorted(bad)


# ---------------------------------------------------------------- 實際收集


def _collect_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _COLLECT_DROP_ENV}


def collect_pytest_ids(roots: Iterable[str] | None = None, cwd: Path = REPO_ROOT) -> set[str]:
    paths = [r for r in (converted_roots() if roots is None else roots) if (cwd / r).exists()]
    p = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *paths],
                       cwd=cwd, env=_collect_env(), capture_output=True, text=True)
    if p.returncode != 0:
        raise GapFileError(f"pytest 收集失敗(rc={p.returncode}):{p.stdout[-1500:]}{p.stderr[-500:]}")
    return {line.strip() for line in p.stdout.splitlines() if "::" in line and not line.startswith(" ")}


def scenario_ids(report: list) -> set[str]:
    return {f'{feat["location"].rsplit(":", 1)[0]}::{el["name"]}'
            for feat in report for el in feat.get("elements", []) if el.get("type") == "scenario"}


def behave_roots(cwd: Path = REPO_ROOT) -> list[str]:
    return [r for r in BEHAVE_ROOT_CANDIDATES if (cwd / r / "steps").is_dir()]


def collect_behave_ids(roots: Iterable[str] | None = None, cwd: Path = REPO_ROOT) -> set[str]:
    out: set[str] = set()
    for root in (behave_roots(cwd) if roots is None else roots):
        with tempfile.TemporaryDirectory() as tmp:
            js = Path(tmp) / "dry.json"
            p = subprocess.run([sys.executable, "-m", "behave", "--dry-run", "-f", "json", "-o", str(js), root],
                               cwd=cwd, env=_collect_env(), capture_output=True, text=True)
            if not js.is_file():
                raise GapFileError(f"behave 收集失敗(rc={p.returncode}):{p.stdout[-1500:]}{p.stderr[-500:]}")
            out |= scenario_ids(json.loads(js.read_text(encoding="utf-8")))
    return out


# ---------------------------------------------------------------- 由收集結果產生清單


def render_gaps(kind: str, ids: Iterable[str], collected: Collection[str], *, reason: str, batch: str, since: str) -> str:
    wanted = list(ids)
    unknown = [i for i in wanted if i not in collected]
    if unknown:
        raise GapFileError(f"不存在於實際收集結果:{unknown}")
    q = lambda s: json.dumps(s, ensure_ascii=False)     # noqa: E731 — JSON 字串即 TOML basic string
    text = "\n".join(f'[[gap]]\nkind = {q(kind)}\nid = {q(i)}\nreason = {q(reason)}\nbatch = {q(batch)}\nsince = {q(since)}\n'
                     for i in wanted)
    parse_gaps(tomllib.loads(text), "render")
    return text


# ---------------------------------------------------------------- pytest hook

_IMPL_KEY = pytest.StashKey[str]()
_APPLIED_KEY = pytest.StashKey[tuple[int, int]]()
_GAP_KEY = pytest.StashKey[Gap]()


def pytest_configure(config: pytest.Config) -> None:
    try:
        config.stash[_IMPL_KEY] = spillway_impl()
    except ImplConfigError as exc:
        raise pytest.UsageError(str(exc)) from exc


def _apply_rust_lists(config: pytest.Config, items: list[pytest.Item]) -> None:
    gap_list = load_gaps(RUST_GAPS_FILE)
    expanded, _ = expand_protected(load_protected(PROTECTED_FILE), {i.nodeid for i in items})
    hits = protected_hits(gap_list, {i for v in expanded.values() for i in v})
    if hits:
        raise GapFileError(f"受保護的測試不得列入差距清單:{hits}")
    by_id = {g.id: g for g in gap_list if g.kind == "pytest"}
    applied = 0
    for item in items:
        gap = by_id.get(item.nodeid)
        if gap is not None:
            item.add_marker(pytest.mark.xfail(strict=True, raises=XFAIL_RAISES, reason=f"rust gap: {gap.batch}"))
            item.stash[_GAP_KEY] = gap
            applied += 1
    entries = load_impl_dependent(IMPL_DEPENDENT_FILE)
    dropped = [i for i in items if any(e.action == "not_applicable" and e.covers(i.nodeid) for e in entries)]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        gone = set(map(id, dropped))
        items[:] = [i for i in items if id(i) not in gone]
    config.stash[_APPLIED_KEY] = (applied, len(dropped))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.stash[_IMPL_KEY] != "rust":
        return
    try:
        _apply_rust_lists(config, items)
    except GapFileError as exc:
        raise pytest.UsageError(str(exc)) from exc


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Iterator[None]:
    outcome = yield
    rep = outcome.get_result()
    gap = item.stash.get(_GAP_KEY, None)
    if gap is None or not rep.skipped:
        return
    if rep.when == "call" and hasattr(rep, "wasxfail"):
        item.stash[_FAILED_KEY] = True     # 預期失敗比照失敗:serve_factory 收尾時 kill(),不做 stop() 的斷言
    text = "" if call.excinfo is None else str(call.excinfo.value)
    if not hasattr(rep, "wasxfail") or (gap.expect is not None and gap.expect not in text):
        rep.outcome = "failed"       # 被 skip 或失敗特徵不符:這不是登記的那個差距
        if hasattr(rep, "wasxfail"):
            del rep.wasxfail


def pytest_terminal_summary(terminalreporter: Any, config: pytest.Config) -> None:
    if config.stash.get(_IMPL_KEY, None) == "rust":
        applied, dropped = config.stash.get(_APPLIED_KEY, (0, 0))
        terminalreporter.write_line(f"spillway impl=rust gaps applied={applied} not_applicable deselected={dropped}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tests.parity.gaps")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--kind", choices=KINDS, required=True)
    r.add_argument("--ids-file", type=Path, required=True)
    r.add_argument("--reason", required=True)
    r.add_argument("--batch", required=True)
    r.add_argument("--since", required=True)
    a = ap.parse_args(argv)
    collected = collect_pytest_ids() if a.kind == "pytest" else collect_behave_ids()
    ids = [line.strip() for line in a.ids_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    sys.stdout.write(render_gaps(a.kind, ids, collected, reason=a.reason, batch=a.batch, since=a.since))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

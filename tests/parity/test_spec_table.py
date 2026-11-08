"""spec 15 的表格與 parity 程式常數比對(CHG-20261108-01 T3;AM-6、AM-10;比照 `test_tables_and_columns_match_structure_docs`)。

解析 `docs/spec.md` 的 15.2、15.3、15.4、15.8、15.9.5,與 `normalize.py` 的常數比對;每個「已定義」比對面必須有對應的語料情境與突變案例。
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.parity import normalize, test_mutation
from tests.parity.corpus import SCENARIOS

SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec.md"


def _section(heading: str) -> str:
    text = SPEC.read_text(encoding="utf-8")
    start = text.index(heading)
    next_h = re.search(r"^#{2,3} ", text[start + len(heading):], re.M)
    return text[start: start + len(heading) + next_h.start()] if next_h else text[start:]


def _tables(section: str) -> list[list[list[str]]]:
    out: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in section.splitlines():
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            current.append(cells)
        elif current:
            out.append(current)
            current = []
    if current:
        out.append(current)
    return [t[2:] for t in out]     # 去掉表頭與分隔列


def _ticks(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


# ---------------------------------------------------------------- 15.2


def _surface_rows() -> list[list[str]]:
    return _tables(_section("### 15.2"))[0]


def test_surfaces_match_spec():
    defined = {r[0]: r[4].strip("`") for r in _surface_rows() if r[3] == "已定義"}
    assert defined == normalize.SURFACES
    undefined = {r[0] for r in _surface_rows() if r[3] != "已定義"}
    assert undefined == set(normalize.UNDEFINED_SURFACES)


def test_every_defined_surface_has_scenario_and_mutation_case():
    by_name = {s.name: s for s in SCENARIOS}
    for surface in normalize.SURFACES.values():
        assert surface in by_name, f"缺語料情境 {surface}"
        assert surface in by_name[surface].surfaces, f"情境 {surface} 沒有宣告同名比對面"
        assert test_mutation.SURFACE_CASES.get(surface), f"缺突變案例 {surface}"


# ---------------------------------------------------------------- 15.3


def test_upstream_required_table_matches_spec():
    rows = _tables(_section("### 15.3"))[0]
    got = {}
    for req, names in rows:
        tick = _ticks(req)[0]
        credential = None if tick == "GET" else ("沒有" not in req)
        got[(tick, credential)] = tuple(_ticks(names))
    assert got == normalize.REQUIRED_UPSTREAM_HEADERS


def test_upstream_ignored_table_matches_spec():
    rows = _tables(_section("### 15.3"))[1]
    got = {cond: tuple(_ticks(names)) for cond, names in rows}
    assert got["一律"] == normalize.IGNORED_UPSTREAM_HEADERS
    assert got["成員沒有 `credential_ref`"] == normalize.IGNORED_WITHOUT_CREDENTIAL


# ---------------------------------------------------------------- 15.4


def test_contract_headers_match_spec():
    got = []
    for names, scope, method in _tables(_section("### 15.4"))[0]:
        for name in _ticks(names):
            path = "/v1/chat/completions" if "/v1/chat/completions" in scope else None
            how = "est_cost" if "形狀" in method else ("verbatim_except" if "除外" in scope else "verbatim")
            got.append((name, path, how))
    assert tuple(got) == normalize.CONTRACT_HEADERS


# ---------------------------------------------------------------- 15.8


def test_db_tables_match_spec():
    text = _section("### 15.8")
    para = next(ln for ln in text.splitlines() if ln.startswith("15.8.1"))
    assert tuple(t for t in _ticks(para) if re.fullmatch(r"[a-z_]+", t)) == normalize.DB_TABLES


def _db_kind(method: str) -> str:
    if method.startswith("先依 15.9.5"):
        return "api_base_literal"
    if method == "literal" or method.startswith("逐字") or method == "15.6.1":
        return "literal"
    if "解析後" in method:
        return "payload_request"
    if method.startswith("整數"):
        return "integer"
    if "比形狀" in method:
        return "sha256"
    if method == "15.6.2":
        return "latency"
    if method == "15.7.4":
        return "presence"
    if "typeof()" in method:
        return "type_only"
    raise AssertionError(f"未知的方式:{method}")


def test_db_column_rules_match_spec():
    got = {}
    for cols, method in _tables(_section("### 15.8"))[0]:
        kind = _db_kind(method)
        if "其餘鍵" in cols:
            got["settings.*"] = kind
            continue
        table = None
        for tick in _ticks(cols):
            if "." in tick:
                table = tick.split(".")[0]
                got[tick] = kind
            else:
                got[f"{table}.{tick}"] = kind
    assert got == normalize.DB_COLUMN_RULES


# ---------------------------------------------------------------- 15.9.5


def _surfaces(cell: str, previous: list[str]) -> list[str]:
    if cell == "同上":
        return previous
    if cell == "DB":
        return ["DB"]
    ticks = _ticks(cell)
    out = []
    for i, tick in enumerate(ticks):
        if " " not in tick:      # 只有方法,路徑沿用下一項(例如 `GET`、`PUT /api/deployments/{id}`)
            tick = f"{tick} {ticks[i + 1].split(' ', 1)[1]}"
        out.append(tick)
    return out


def _paths(cell: str, surface: list[str]) -> list[str]:
    ticks = _ticks(cell)
    if surface == ["DB"]:
        return [ticks[0] + ticks[1]]
    if "--port" in ticks:
        return [ticks[0]]
    out = []
    prefix = "$."
    for tick in ticks:
        if tick.startswith("$"):
            prefix = tick.rsplit(".", 1)[0] + "."
            out.append(tick)
        else:
            out.append(prefix + tick)
    return out


def test_path_rules_match_spec():
    want: set[tuple[str, str, str, str]] = set()
    surface: list[str] = []
    for cell, paths, shape, treatment in _tables(_section("### 15.9"))[0]:
        surface = _surfaces(cell, surface)
        for s in surface:
            for p in _paths(paths, surface):
                want.add((s, p, shape, treatment))
    assert want == {(r.surface, r.path, r.shape, r.treatment) for r in normalize.PATH_RULES}

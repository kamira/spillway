"""Python 對 Python 自我比對(CHG-20261108-01 T3 L2;AM-10):只證明正規化不過窄。"""
from __future__ import annotations

import pytest

from tests.parity.corpus import SCENARIOS
from tests.parity.normalize import PATH_RULES, DB_SURFACE, route_of, _parse_body, _tokens, _visit
from tests.parity.runner import run_corpus, run_pair, run_scenario


@pytest.mark.parametrize("round_no", [1, 2], ids=["round1", "round2"])
@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_self_compare_has_zero_differences(scenario, round_no):
    result = run_pair(scenario)
    assert not result.empty_a and not result.empty_b, (result.empty_a, result.empty_b)
    assert result.report.ok, result.report.text()


def test_origin_rejection_reaches_the_corpus():
    """15.4 的 403 例外要有語料走到(`host_not_allowed` 維持 comparator 層:突變 wrapper 會改寫 Host)。"""
    capture = run_scenario(next(s for s in SCENARIOS if s.name == "errors"))
    bodies = [body for ex in capture.exchanges if ex.status == 403 for kind, body in [_parse_body(ex)] if kind == "json"]
    assert any(b.get("error", {}).get("code") == "origin_not_allowed" for b in bodies)


def test_every_path_rule_is_reached_by_the_corpus():
    """15.9.5 的每條路徑至少有一個語料回應走到(語料退化時規則會變成空轉);資料庫列由 `db_rows` 情境涵蓋。"""
    reached: set[tuple[str, str]] = set()
    for result in run_corpus():
        for ex in result.capture_a.exchanges:
            kind, body = _parse_body(ex)
            surface = route_of(ex.method, ex.path)
            if kind != "json" or surface is None:
                continue
            for rule in PATH_RULES:
                if rule.surface == surface and next(_visit(body, _tokens(rule.path), "$", None, None), None) is not None:
                    reached.add((rule.surface, rule.path))
    missing = sorted({(r.surface, r.path) for r in PATH_RULES if r.surface != DB_SURFACE} - reached)
    assert not missing, missing

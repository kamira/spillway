from __future__ import annotations

import pytest

from spillway.adapters.base.effort import effort_to_upstream
from spillway.adapters.base.manifest import AdapterManifest
from spillway.effort import EffortMapper

ALL = ["none", "low", "medium", "high", "max"]
pick = EffortMapper.pick


def test_route_target_wins_over_request():
    assert pick("high", "low", ALL) == "high"


def test_request_used_when_route_target_empty():
    assert pick(None, "low", ALL) == "low"
    assert pick("", "medium", ALL) == "medium"


def test_both_empty_returns_none():
    assert pick(None, None, ALL) is None
    assert pick("", "", ALL) is None


def test_degrade_max_to_medium():
    assert pick("max", None, ["low", "medium"]) == "medium"


def test_no_lower_level_returns_none():
    assert pick("low", None, ["high"]) is None


def test_supported_level_unchanged_and_none_is_lowest():
    assert pick("medium", None, ["medium"]) == "medium"
    assert pick("low", None, ["none", "high"]) == "none"
    assert pick("none", None, ["low"]) is None


def test_caps_none_means_unrestricted_and_empty_means_unsupported():
    assert pick("max", None, None) == "max"
    assert pick("max", None, []) is None


def test_invalid_levels_raise():
    with pytest.raises(ValueError):
        pick("extreme", None, ALL)
    with pytest.raises(ValueError):
        pick(None, "turbo", ALL)
    with pytest.raises(ValueError):
        pick("low", None, ["low", "bogus"])


def test_accepts_tuple_caps():
    assert pick("max", None, ("low", "high")) == "high"


def _manifest() -> AdapterManifest:
    return AdapterManifest(
        providers=("fake",), kinds=("api",), access=("api_key",), cardinality="single",
        effort_map={"none": None, "low": "L", "medium": "M", "high": "H", "max": None},
        default_signals={}, default_capabilities={}, default_params={},
        requires=(), contract_version=1,
    )


@pytest.mark.parametrize(
    "target,req,caps,expected_level,expected_upstream",
    [
        ("high", "low", ALL, "high", "H"),
        (None, "medium", ALL, "medium", "M"),
        ("max", None, ["low", "medium"], "medium", "M"),
        ("max", None, ALL, "max", None),      # effort_map 將 max 對應為 null
        ("low", None, ["high"], None, None),
        (None, None, ALL, None, None),
    ],
)
def test_pick_through_effort_to_upstream(target, req, caps, expected_level, expected_upstream):
    m = _manifest()
    level = pick(target, req, caps)
    assert level == expected_level
    assert effort_to_upstream(m, level) == expected_upstream
    if level is not None:
        assert effort_to_upstream(m, level) == m.effort_map[level]

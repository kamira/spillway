"""domain 常數集中(CHG-20261101-02 T1;決策 6)。"""
from __future__ import annotations

import re
import typing
from pathlib import Path

import spillway
from spillway import domain
from spillway.domain import (
    CALL_STATUSES,
    CLOUD_KINDS,
    COOLING_STATUSES,
    KINDS,
    LEDGER_STATUSES,
    STATUS_SKIPPED_PRECHECK,
)

SCHEMA = Path(spillway.__file__).parent / "store" / "schema.sql"


def test_cloud_kinds_value_and_subset_of_kinds():
    assert CLOUD_KINDS == ("cli", "api")
    assert set(CLOUD_KINDS) <= set(KINDS)


def test_ledger_statuses_is_frozenset_and_reexported_by_ledger():
    import spillway.ledger as ledger

    assert isinstance(LEDGER_STATUSES, frozenset)
    assert ledger.LEDGER_STATUSES is domain.LEDGER_STATUSES
    assert "LEDGER_STATUSES" in ledger.__all__


def test_ledger_statuses_are_call_statuses_plus_skipped_precheck():
    assert STATUS_SKIPPED_PRECHECK == "skipped_precheck"
    assert LEDGER_STATUSES == frozenset(CALL_STATUSES) | {STATUS_SKIPPED_PRECHECK}


def test_cooling_statuses_value_and_subset():
    assert COOLING_STATUSES == ("quota_exhausted", "rate_limited")
    assert set(COOLING_STATUSES) <= LEDGER_STATUSES
    assert set(COOLING_STATUSES) <= set(CALL_STATUSES)


def test_routing_cloud_kinds_is_domain_object():
    import spillway.routing as routing
    import spillway.routing.filter as filt
    import spillway.routing.selector as selector

    assert routing.CLOUD_KINDS is domain.CLOUD_KINDS
    assert "CLOUD_KINDS" in routing.__all__
    assert filt.CLOUD_KINDS is domain.CLOUD_KINDS
    assert selector.CLOUD_KINDS is domain.CLOUD_KINDS


def test_cloud_kinds_defined_only_in_domain():
    src = Path(spillway.__file__).parent
    offenders = [
        str(p.relative_to(src)) for p in src.rglob("*.py")
        if p.name != "domain.py" and re.search(r"^\s*CLOUD_KINDS\s*(:[^=]*)?=", p.read_text("utf-8"), re.M)
    ]
    assert offenders == []


def test_admin_literals_match_domain_constants():
    from spillway.api import admin

    args = typing.get_args(admin.StatusLit)
    assert len(args) == len(LEDGER_STATUSES)
    assert set(args) == LEDGER_STATUSES
    kind_ann = admin.DeploymentIn.model_fields["kind"].annotation
    assert typing.get_args(kind_ann) == KINDS


def test_schema_kind_check_matches_kinds():
    text = SCHEMA.read_text("utf-8")
    m = re.search(r"kind\s+TEXT NOT NULL CHECK \(kind IN \(([^)]*)\)\)", text)
    assert m, "schema.sql 找不到 kind CHECK"
    values = tuple(v.strip().strip("'") for v in m.group(1).split(","))
    assert values == KINDS

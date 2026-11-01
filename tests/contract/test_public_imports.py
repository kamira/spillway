"""公開匯入路徑保留(CHG-20261101-02 T1;決策 6、7、8;Global Constraints「公開匯入路徑保留」)。"""
from __future__ import annotations

import spillway.domain as domain
import spillway.effort as effort
import spillway.ledger as ledger
import spillway.redact as redact
import spillway.routing as routing
import spillway.routing.router as router


def test_normalize_effort_reexports_are_same_object():
    assert routing.normalize_effort is effort.normalize_effort
    assert router.normalize_effort is effort.normalize_effort
    assert routing.InvalidEffort is effort.InvalidEffort
    assert router.InvalidEffort is effort.InvalidEffort


def test_router_all_unchanged():
    assert sorted(router.__all__) == sorted([
        "AttemptInfo", "InvalidEffort", "Plan", "PlanEntry", "PlanExcluded", "PlanSlot",
        "RouteOutcome", "Router", "normalize_effort",
    ])
    for name in router.__all__:
        assert name in routing.__all__
        assert getattr(routing, name) is getattr(router, name)


def test_effort_module_exports():
    for name in ("EffortMapper", "InvalidEffort", "normalize_effort"):
        assert name in effort.__all__


def test_ledger_and_routing_constant_reexports():
    assert ledger.LEDGER_STATUSES is domain.LEDGER_STATUSES
    assert "LEDGER_STATUSES" in ledger.__all__
    assert routing.CLOUD_KINDS is domain.CLOUD_KINDS
    assert "CLOUD_KINDS" in routing.__all__


def test_redact_exports_shared_instance():
    assert "REDACTOR" in redact.__all__
    assert isinstance(redact.REDACTOR, redact.Redactor)

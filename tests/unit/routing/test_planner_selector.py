"""T11 L1/L2:OrderPlanner(spec 4.3)與 SlotSelector(spec 4.4;決策 6)。"""
from __future__ import annotations

import pytest

from spillway.clock import SeqRng, SystemRng
from spillway.domain import Budget
from spillway.estimate import Estimate
from spillway.quota import QuotaService
from spillway.routing import Candidate, OrderPlanner, Slot, SlotEntry, SlotSelector, WEIGHT_VALUES
from spillway.store import repo
from spillway.store.repo import LedgerRow, RouteTarget

from .helpers import make_dep

EST = Estimate(1, 500, 2000, None)


def cands(db, spec):
    """spec: [(id, kind)] → 建成員並包成 Candidate(依給定順序)。"""
    out = []
    for dep_id, kind in spec:
        dep = repo.load_deployment(db, dep_id) or make_dep(db, dep_id, kind=kind)
        out.append(Candidate(deployment=dep, estimate=EST,
                             health="healthy" if kind == "local" else None))
    return out


def layout(slots: list[Slot]):
    return [(s.number, [(e.deployment.id, e.unsorted) for e in s.entries]) for s in slots]


# ------------------------------------------------------------------ OrderPlanner


def test_default_order_cli_api_then_each_local_by_id(db):
    cs = cands(db, [("zeta-local", "local"), ("api-b", "api"), ("sub", "cli"),
                    ("api-a", "api"), ("alpha-local", "local")])
    slots = OrderPlanner().plan((), cs)
    assert layout(slots) == [
        (1, [("sub", False)]),
        (2, [("api-a", False), ("api-b", False)]),
        (3, [("alpha-local", False)]),
        (4, [("zeta-local", False)]),
    ]
    for s in slots:
        for i, e in enumerate(s.entries):
            assert e.weight == "medium" and e.effort is None and e.position == i
            assert e.estimate == EST


def test_default_numbering_is_stable_when_members_filtered(db):
    # members 給了完整成員:被篩掉的 cli 仍佔 slot 1,api 維持 slot 2(dry-run 與 header 穩定)
    cs = cands(db, [("sub", "cli"), ("api-a", "api"), ("loc", "local")])
    members = [c.deployment for c in cs]
    slots = OrderPlanner().plan((), cs[1:], members=members)
    assert layout(slots) == [(2, [("api-a", False)]), (3, [("loc", False)])]


def test_default_without_cli_members_starts_api_at_slot_1(db):
    # 名稱沿用;M16 後的語意:層級編號固定,沒有 cli 成員時 slot 1 空著(不輸出),api 仍為 slot 2
    cs = cands(db, [("api-a", "api"), ("loc", "local")])
    assert layout(OrderPlanner().plan((), cs)) == [(2, [("api-a", False)]), (3, [("loc", False)])]


def test_default_numbering_fixed_without_api_members(db):
    # 只有 cli 與 local:api 層 slot 2 空著不輸出,local 依 id 從 slot 3 起
    cs = cands(db, [("sub", "cli"), ("loc-b", "local"), ("loc-a", "local")])
    assert layout(OrderPlanner().plan((), cs)) == [
        (1, [("sub", False)]), (3, [("loc-a", False)]), (4, [("loc-b", False)])]


def test_default_numbering_only_locals(db):
    cs = cands(db, [("loc", "local")])
    assert layout(OrderPlanner().plan((), cs)) == [(3, [("loc", False)])]


def test_unsorted_numbering_fixed_without_cli_members(db):
    # 有手動排序:base = max(手動 slot)+1 = 3;沒有 cli 新成員時 api 新成員仍為 base+1
    cs = cands(db, [("api-a", "api"), ("api-new", "api"), ("loc-new", "local")])
    rts = [RouteTarget("api-a", 2, 0)]
    assert layout(OrderPlanner().plan(rts, cs)) == [
        (2, [("api-a", False)]), (4, [("api-new", True)]), (5, [("loc-new", True)])]


def test_local_numbering_uses_members_not_candidates(db):
    # 被篩掉的 local(loc-a)仍佔它的編號,loc-b 不往前遞補
    cs = cands(db, [("api-a", "api"), ("loc-a", "local"), ("loc-b", "local")])
    members = [c.deployment for c in cs]
    slots = OrderPlanner().plan((), [cs[0], cs[2]], members=members)
    assert layout(slots) == [(2, [("api-a", False)]), (4, [("loc-b", False)])]


def test_manual_order_by_slot_and_position(db):
    cs = cands(db, [("api-a", "api"), ("api-b", "api"), ("api-c", "api"), ("loc", "local")])
    rts = [RouteTarget("api-c", 2, 0, "low", "low"),
           RouteTarget("api-b", 1, 1, None, "low"),
           RouteTarget("api-a", 1, 0, "high", "high"),
           RouteTarget("loc", 1, 2)]
    slots = OrderPlanner().plan(rts, cs)
    assert layout(slots) == [(1, [("api-a", False), ("api-b", False), ("loc", False)]),
                             (2, [("api-c", False)])]
    a = slots[0].entries[0]
    assert (a.weight, a.effort, a.position) == ("high", "high", 0)
    assert slots[1].entries[0].effort == "low"


def test_new_members_appended_after_manual_slots_and_marked_unsorted(db):
    cs = cands(db, [("api-a", "api"), ("api-new", "api"), ("sub-new", "cli"),
                    ("loc-new", "local"), ("api-c", "api")])
    rts = [RouteTarget("api-a", 1, 0, None, "high"), RouteTarget("api-c", 3, 0)]
    slots = OrderPlanner().plan(rts, cs)
    assert layout(slots) == [
        (1, [("api-a", False)]),
        (3, [("api-c", False)]),
        (4, [("sub-new", True)]),
        (5, [("api-new", True)]),
        (6, [("loc-new", True)]),
    ]
    assert all(e.weight == "medium" for s in slots[2:] for e in s.entries)


def test_filtered_members_absent_and_empty_slots_removed(db):
    cs = cands(db, [("api-a", "api"), ("api-b", "api"), ("api-c", "api")])
    rts = [RouteTarget("api-a", 1, 0), RouteTarget("api-b", 1, 1), RouteTarget("api-c", 2, 0)]
    # api-a、api-b 被篩掉(冷卻):slot 1 消失,slot 2 保留原編號(AC-3 的 X-Spillway-Slot=2)
    slots = OrderPlanner().plan(rts, [cs[2]])
    assert layout(slots) == [(2, [("api-c", False)])]


def test_route_targets_for_non_candidates_ignored(db):
    cs = cands(db, [("api-a", "api")])
    rts = [RouteTarget("gone", 1, 0), RouteTarget("api-a", 2, 0)]
    assert layout(OrderPlanner().plan(rts, cs)) == [(2, [("api-a", False)])]


# ------------------------------------------------------------------ SlotSelector


def mkslot(db, spec, number=1):
    """spec: [(id, kind, weight, position)]。"""
    entries = []
    for dep_id, kind, weight, pos in spec:
        dep = repo.load_deployment(db, dep_id) or make_dep(db, dep_id, kind=kind)
        entries.append(SlotEntry(deployment=dep, weight=weight, effort=None, position=pos,
                                 unsorted=False, estimate=EST, health=None))
    return Slot(number=number, entries=tuple(entries))


def drain(it):
    return [e.deployment.id for e in it]


def test_weight_values():
    assert WEIGHT_VALUES == {"high": 3, "medium": 2, "low": 1}


def test_seq_rng_order_is_predictable(db, clock):
    slot = mkslot(db, [("A", "api", "high", 0), ("B", "api", "medium", 1), ("C", "api", "low", 2)])
    q = QuotaService(db, clock)
    # 權重 3,2,1(總 6):0.6×6=3.6 → B;剩 A3、C1(總 4):0.9×4=3.6 → C;剩 A
    assert drain(SlotSelector(q, SeqRng([0.6, 0.9])).iterate(slot, clock.now())) == ["B", "C", "A"]
    # 0.0 → A;剩 B2、C1:0.5×3=1.5 → B;剩 C
    assert drain(SlotSelector(q, SeqRng([0.0, 0.5])).iterate(slot, clock.now())) == ["A", "B", "C"]


def test_ratio_scales_weight(db, clock):
    make_dep(db, "A", budget=Budget("usd", 10.0, "daily"))
    make_dep(db, "B", budget=Budget("usd", 10.0, "daily"))
    repo.insert_ledger(db, LedgerRow(ts=clock.now(), status="ok", deployment_id="A", cost_usd=7.5))
    slot = mkslot(db, [("A", "api", "high", 0), ("B", "api", "low", 1)])
    # A:3×0.25=0.75;B:1×1=1;總 1.75。0.4×1.75=0.7 → A;0.5×1.75=0.875 → B
    q = QuotaService(db, clock)
    assert drain(SlotSelector(q, SeqRng([0.4])).iterate(slot, clock.now()))[0] == "A"
    assert drain(SlotSelector(q, SeqRng([0.5])).iterate(slot, clock.now()))[0] == "B"


def test_all_zero_weight_falls_back_to_position(db, clock):
    make_dep(db, "A", budget=Budget("usd", 0.0, "daily"))
    make_dep(db, "B", budget=Budget("usd", 0.0, "daily"))
    slot = mkslot(db, [("B", "api", "high", 0), ("A", "api", "high", 1)])
    order = drain(SlotSelector(QuotaService(db, clock), SeqRng([0.99])).iterate(slot, clock.now()))
    assert order == ["B", "A"]


def test_zero_weight_member_tried_after_positive_ones(db, clock):
    make_dep(db, "Z", budget=Budget("usd", 0.0, "daily"))
    slot = mkslot(db, [("Z", "api", "high", 0), ("P", "api", "low", 1)])
    order = drain(SlotSelector(QuotaService(db, clock), SeqRng([0.0])).iterate(slot, clock.now()))
    assert order == ["P", "Z"]


def test_failed_member_not_redrawn_and_reweights_remaining(db, clock):
    slot = mkslot(db, [("A", "api", "high", 0), ("B", "api", "medium", 1), ("C", "cli", "low", 2)])
    it = SlotSelector(QuotaService(db, clock), SystemRng(seed=3)).iterate(slot, clock.now())
    first = next(it)
    rest = [e.deployment.id for e in it]
    assert first.deployment.id not in rest
    assert sorted([first.deployment.id, *rest]) == ["A", "B", "C"]


def test_locals_after_cloud_by_position(db, clock):
    slot = mkslot(db, [("L2", "local", "medium", 0), ("A", "api", "low", 3),
                       ("L1", "local", "high", 1), ("S", "cli", "high", 2)])
    order = drain(SlotSelector(QuotaService(db, clock), SeqRng([0.0])).iterate(slot, clock.now()))
    assert order[2:] == ["L2", "L1"]
    assert set(order[:2]) == {"A", "S"}


def test_order_helper_returns_full_list(db, clock):
    slot = mkslot(db, [("A", "api", "high", 0), ("L", "local", "medium", 1)])
    sel = SlotSelector(QuotaService(db, clock), SeqRng([0.0]))
    assert [e.deployment.id for e in sel.order(slot, clock.now())] == ["A", "L"]


def test_high_vs_low_ratio_10000_draws_seed_7(db, clock):
    slot = mkslot(db, [("A", "api", "high", 0), ("B", "api", "low", 1)])
    sel = SlotSelector(QuotaService(db, clock), SystemRng(seed=7))
    now = clock.now()
    n = 10_000
    a = sum(1 for _ in range(n) if next(sel.iterate(slot, now)).deployment.id == "A")
    assert 0.73 <= a / n <= 0.77

"""T11 L1:target 比對(members_of)與 Resolver(spec 1.2、2.3、4.1;決策 18)。"""
from __future__ import annotations

import dataclasses

import pytest

from spillway.routing import Resolver, RouteNotFound, Target, members_of
from spillway.store import repo
from spillway.store.repo import Group, Route, RouteTarget

from .helpers import make_dep


# ------------------------------------------------------------------ members_of


@pytest.fixture
def deps(db):
    return [
        make_dep(db, "a", tags=("fast", "cheap")),
        make_dep(db, "b", tags=("fast",)),
        make_dep(db, "c", tags=("cheap", "beta")),
        make_dep(db, "d", tags=()),
    ]


def _ids(ds):
    return [d.id for d in ds]


def test_tags_all_requires_every_tag(deps):
    assert _ids(members_of({"tags": {"all": ["fast", "cheap"]}}, deps)) == ["a"]


def test_tags_any_requires_at_least_one(deps):
    assert _ids(members_of({"tags": {"any": ["beta", "fast"]}}, deps)) == ["a", "b", "c"]


def test_tags_any_empty_means_no_constraint(deps):
    assert _ids(members_of({"tags": {"all": [], "any": [], "none": []}}, deps)) == ["a", "b", "c", "d"]
    assert _ids(members_of({"tags": {}}, deps)) == ["a", "b", "c", "d"]


def test_tags_none_excludes_any_listed_tag(deps):
    assert _ids(members_of({"tags": {"none": ["beta", "fast"]}}, deps)) == ["d"]


def test_tags_combined_all_any_none(deps):
    sel = {"tags": {"all": ["cheap"], "any": ["fast", "beta"], "none": ["beta"]}}
    assert _ids(members_of(sel, deps)) == ["a"]


def test_group_reference_uses_group_selector(deps):
    groups = [Group("fasties", {"all": ["fast"], "any": [], "none": ["cheap"]})]
    assert _ids(members_of({"group": "fasties"}, deps, groups)) == ["b"]


def test_group_reference_accepts_mapping(deps):
    assert _ids(members_of({"group": "g"}, deps, {"g": {"any": ["beta"]}})) == ["c"]


def test_missing_group_matches_nothing(deps):
    assert members_of({"group": "nope"}, deps, []) == []


def test_unknown_target_shape_raises(deps):
    with pytest.raises(ValueError):
        members_of({"bogus": 1}, deps)


# ------------------------------------------------------------------ Resolver


@pytest.fixture
def world(db):
    make_dep(db, "api-a", kind="api", model_name="gpt", tags=("review",))
    make_dep(db, "api-b", kind="api", model_name="gpt", tags=("review",))
    make_dep(db, "claude-sub", kind="cli", model_name="sonnet", tags=("review",))
    make_dep(db, "qwen-local", kind="local", model_name="qwen2.5", tags=())
    make_dep(db, "api-qwen", kind="api", model_name="qwen-api", tags=())
    repo.save_group(db, Group("reviewers", {"all": ["review"], "any": [], "none": []}))
    repo.save_route(db, Route("review", {"group": "reviewers"}, mode="task",
                              record_context=True, budget_precheck=True))
    repo.replace_route_targets(db, "review", [RouteTarget("api-a", 1, 0, "high", "high")])
    return db


def test_local_prefix_resolves_by_id(world):
    t = Resolver(world).resolve("local/qwen-local")
    assert t.kind == "local_direct"
    assert [d.id for d in t.members] == ["qwen-local"]
    assert t.route_name is None and t.route is None
    assert t.deployment.id == "qwen-local"


def test_local_prefix_resolves_by_model_name(world):
    t = Resolver(world).resolve("local/qwen2.5")
    assert t.kind == "local_direct" and t.deployment.id == "qwen-local"


def test_local_prefix_only_matches_local_kind(world):
    with pytest.raises(RouteNotFound) as ei:
        Resolver(world).resolve("local/api-a")
    assert ei.value.code == "route_not_found"
    with pytest.raises(RouteNotFound):
        Resolver(world).resolve("local/no-such")


def test_route_prefix_and_bare_name_are_equivalent(world):
    r = Resolver(world)
    a, b = r.resolve("route:review"), r.resolve("review")
    assert a.model == "route:review" and b.model == "review"   # 保留請求原字串
    assert dataclasses.replace(a, model="review") == b
    assert a.kind == "route"
    assert a.route_name == "review"
    assert a.route is not None and a.route.name == "review"
    assert a.mode == "task" and a.budget_precheck is True and a.record_context is True
    assert [d.id for d in a.members] == ["api-a", "api-b", "claude-sub"]
    assert [t.deployment_id for t in a.route_targets] == ["api-a"]


def test_route_prefix_with_unknown_route_is_not_found(world):
    with pytest.raises(RouteNotFound):
        Resolver(world).resolve("route:gpt")  # gpt 是 model_name,不是 route


def test_model_name_maps_to_implicit_route(world):
    t = Resolver(world).resolve("gpt")
    assert t.kind == "implicit"
    assert [d.id for d in t.members] == ["api-a", "api-b"]
    assert t.mode == "chat"
    assert t.route is None and t.route_targets == ()
    assert t.route_name == "gpt"          # 決策 18
    assert t.budget_precheck is False and t.record_context is None


def test_route_name_wins_over_model_name(world, db):
    make_dep(db, "x", model_name="review")
    assert Resolver(world).resolve("review").kind == "route"


def test_no_match_raises_route_not_found(world):
    with pytest.raises(RouteNotFound) as ei:
        Resolver(world).resolve("no-such-route")
    assert ei.value.code == "route_not_found"
    assert ei.value.model == "no-such-route"


def test_target_is_frozen(world):
    t = Resolver(world).resolve("gpt")
    assert isinstance(t, Target)
    with pytest.raises(Exception):
        t.mode = "task"  # type: ignore[misc]

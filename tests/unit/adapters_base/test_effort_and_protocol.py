"""effort_to_upstream()、Adapter Protocol 與 AdapterContext(spec 6.3、7.1;決策 5、13)。"""
from __future__ import annotations

import dataclasses
import inspect

import pytest

from spillway.adapters.base import (
    Adapter,
    AdapterContext,
    AdapterManifest,
    CallResult,
    effort_to_upstream,
)
from spillway.clock import FakeClock


def manifest(effort_map) -> AdapterManifest:
    return AdapterManifest(
        providers=("x",), kinds=("api",), access=("api",), cardinality="multiple",
        effort_map=effort_map, default_signals={}, default_capabilities={},
        default_params={}, requires=(), contract_version=1,
    )


API_MAP = {"none": None, "low": "low", "medium": "medium", "high": "high", "max": "max"}
CODEX_MAP = {"none": "minimal", "low": "low", "medium": "medium", "high": "high", "max": "xhigh"}


def test_none_maps_to_null():
    assert effort_to_upstream(manifest(API_MAP), "none") is None


@pytest.mark.parametrize("level", ["low", "medium", "high", "max"])
def test_api_levels(level):
    assert effort_to_upstream(manifest(API_MAP), level) == level


def test_codex_style_map():
    m = manifest(CODEX_MAP)
    assert effort_to_upstream(m, "none") == "minimal"
    assert effort_to_upstream(m, "max") == "xhigh"


def test_level_none_means_not_sent():
    assert effort_to_upstream(manifest(API_MAP), None) is None


def test_unknown_level_raises():
    with pytest.raises(ValueError):
        effort_to_upstream(manifest(API_MAP), "xhigh")


# ---- Protocol / Context ---------------------------------------------------------------

class GoodAdapter:
    async def call(self, dep, req, effort):
        return CallResult(True, "", None, None, None, "ok", None)

    async def health(self, dep):
        return True


class NoHealth:
    async def call(self, dep, req, effort):
        return None


def test_adapter_protocol_runtime_check():
    assert isinstance(GoodAdapter(), Adapter)
    assert not isinstance(NoHealth(), Adapter)


def test_protocol_methods_are_async():
    assert inspect.iscoroutinefunction(Adapter.call)
    assert inspect.iscoroutinefunction(Adapter.health)


def test_adapter_context_fields():
    names = [f.name for f in dataclasses.fields(AdapterContext)]
    assert names == ["credentials", "clock", "cli_runner"]
    ctx = AdapterContext(credentials=lambda ref: "sk-test-0000", clock=FakeClock())
    assert ctx.cli_runner is None
    assert ctx.credentials("env:SPILLWAY_TEST_KEY") == "sk-test-0000"
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.cli_runner = object()  # type: ignore[misc]

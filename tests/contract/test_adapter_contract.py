"""共用合約測試(spec 7.6.8;design「Adapter 模組契約」規則 3;DIR-3 規則 4)。

對 `AdapterRegistry` 發現的每個模組參數化:四個檔案齊全、manifest 欄位完整且合法、
`effort_map` 五級皆有定義、`models.yaml` 可解析且 `efforts` 只含五級、`create(context)`
產出 async `call`/`health` 的實例、import 分層合規。`call` 四種上游輸出的 status 由
`tests/contract/test_call_status.py`(T6)負責。

環境變數 `SPILLWAY_CONTRACT_EXTRA_PATHS`(以 os.pathsep 分隔)可把額外的模組資料夾
一併納入參數化(決策 15)。目前沒有任何內建模組時,下方以 tmp_path 假模組驗證機制本身。
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from spillway.adapters.base import (
    EFFORTS,
    AdapterContext,
    AdapterRegistry,
    Budget,
    CallResult,
    ChatRequest,
    Deployment,
    contract_problems,
)
from spillway.clock import FakeClock
from tests.contract.fakemods import write_module


def _ctx() -> AdapterContext:
    return AdapterContext(credentials=lambda ref: "sk-test-0000", clock=FakeClock())


def _extra_paths() -> list[Path]:
    raw = os.environ.get("SPILLWAY_CONTRACT_EXTRA_PATHS", "")
    return [Path(p) for p in raw.split(os.pathsep) if p]


def _discovered():
    r = AdapterRegistry(_ctx())
    r.discover(extra_paths=_extra_paths())
    return r.modules()


MODULES = _discovered()


def test_modules_discovered():
    """守門:參數化不可因為沒有發現任何模組而全部 skip(波次 1–2 施工審議建議修,T6 加入)。"""
    assert MODULES
    assert {"anthropic_api", "openai_api", "litellm"} <= {m.name for m in MODULES}


@pytest.mark.parametrize("info", MODULES, ids=[m.name for m in MODULES])
def test_module_contract(info):
    assert contract_problems(info, _ctx()) == []


@pytest.mark.parametrize("info", MODULES, ids=[m.name for m in MODULES])
def test_effort_map_has_five_levels(info):
    assert info.manifest is not None, info.reason
    assert tuple(info.manifest.effort_map) == EFFORTS


@pytest.mark.parametrize("info", MODULES, ids=[m.name for m in MODULES])
def test_models_efforts_only_five_levels(info):
    for m in info.models:
        assert set(m.capabilities["efforts"]) <= set(EFFORTS)
        assert {"ctx", "tools", "vision", "efforts"} <= set(m.capabilities)


# ---- 型別欄位(契約清單 ChatRequest/Deployment/CallResult 列)------------------------------

def _names(cls) -> list[str]:
    return [f.name for f in dataclasses.fields(cls)]


def test_value_object_fields():
    assert _names(Deployment) == ["id", "model_name", "kind", "provider", "model", "params",
                                  "credential_ref", "capabilities", "enabled", "tags", "budget"]
    assert _names(ChatRequest) == ["model", "messages", "max_tokens", "reasoning_effort", "tools",
                                   "metadata", "stream", "extra"]
    # spec 7.1 修訂(決策 28):tool_calls、finish_reason 為附加的可選欄位,放在最後
    assert _names(CallResult) == ["ok", "text", "tokens_in", "tokens_out", "cost_usd", "status", "error",
                                  "tool_calls", "finish_reason"]
    legacy = CallResult(True, "t", 1, 1, 0.0, "ok", None)   # 既有的七欄建構呼叫仍可用
    assert legacy.tool_calls is None and legacy.finish_reason is None
    assert _names(Budget) == ["type", "limit_amount", "window", "reset_anchor", "exhausted_until"]


# ---- 機制本身(目前沒有內建模組時仍要能證明合約測試有效)-----------------------------------

def _info(tmp_path, name):
    r = AdapterRegistry(_ctx())
    r.discover(extra_paths=[tmp_path])
    return next(m for m in r.modules() if m.name == name)


def test_good_fake_module_passes(tmp_path):
    write_module(tmp_path, "good_mod", providers=("good",))
    assert contract_problems(_info(tmp_path, "good_mod"), _ctx()) == []


def test_missing_files_reported(tmp_path):
    write_module(tmp_path, "nofiles", providers=("nf",), readme=False,
                 adapter_source="", adapter_file=True)
    (tmp_path / "nofiles" / "adapter.py").unlink()
    # __init__ 從 .adapter 匯入,刪掉後無法 import → 模組不可用,且列出缺檔
    problems = contract_problems(_info(tmp_path, "nofiles"), _ctx())
    joined = "\n".join(problems)
    assert "README.md" in joined and "adapter.py" in joined


def test_invalid_manifest_reported(tmp_path):
    write_module(tmp_path, "badcard", providers=("bc",), cardinality="many")
    problems = contract_problems(_info(tmp_path, "badcard"), _ctx())
    assert any("cardinality" in p for p in problems)


def test_bad_models_yaml_reported(tmp_path):
    write_module(tmp_path, "badmodels", providers=("bm",),
                 models_yaml="- id: a\n  label: A\n  capabilities: {ctx: 1, tools: true, vision: false, efforts: [xhigh]}\n")
    problems = contract_problems(_info(tmp_path, "badmodels"), _ctx())
    assert any("efforts" in p for p in problems)


def test_sync_methods_reported(tmp_path):
    write_module(tmp_path, "syncy", providers=("sy",), adapter_source=(
        "class FakeAdapter:\n"
        "    def __init__(self, c):\n        pass\n"
        "    async def call(self, dep, req, effort):\n        return None\n"
        "    def health(self, dep):\n        return True\n"))
    problems = contract_problems(_info(tmp_path, "syncy"), _ctx())
    assert any("health" in p for p in problems)


def test_layering_violation_reported(tmp_path):
    write_module(tmp_path, "leaky", providers=("lk",),
                 extra_files={"helper.py": "import litellm\n"})
    problems = contract_problems(_info(tmp_path, "leaky"), _ctx())
    assert any("litellm" in p for p in problems)


def test_extra_paths_env_is_parametrized(tmp_path, monkeypatch):
    write_module(tmp_path, "envmod", providers=("envp",))
    monkeypatch.setenv("SPILLWAY_CONTRACT_EXTRA_PATHS", str(tmp_path))
    assert "envmod" in {m.name for m in _discovered()}

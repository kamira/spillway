"""共用合約測試:`call()` 對四種上游輸出回傳對應的 `CallResult.status`(spec 7.6.8、5.3.1;
design「Adapter 模組契約」規則 3;CHG-20261030-02 T6)。

對 `AdapterRegistry` 發現的每個模組參數化(含 `SPILLWAY_CONTRACT_EXTRA_PATHS` 的額外路徑)。
每個模組需要一組「怎麼對假上游呼叫它」的設定(harness):

- 三個內建 API 模組共用 `tests/fakes/api_upstream/`(D17),設定寫在下方 `BUILTIN_HARNESS`。
- 其他模組:在 `tests/fakes/<模組名>/__init__.py` 提供 `call_status_harness(fake_api)`,回傳
  `[(Deployment, set_mode), ...]`,`set_mode(mode)` 讓該模組的假上游進入 `ok`、`http_402`、
  `insufficient_quota`、`http_429`、`http_500` 之一。
- repo 外的模組(經 `SPILLWAY_CONTRACT_EXTRA_PATHS` 納入):`tests.fakes.<模組名>` 找不到時,讀
  `SPILLWAY_CONTRACT_HARNESS_PATHS`(os.pathsep 分隔)目錄下的 `<模組名>_harness.py`,
  取其 `call_status_harness(fake_api)`(末端審查 M29)。
沒有 harness 的模組直接 fail。
"""
from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path

import pytest

from spillway.adapters.base import AdapterContext, AdapterRegistry, ChatRequest, Deployment
from spillway.clock import FakeClock

pytestmark = pytest.mark.anyio

KEY = "sk-test-0000"
UPSTREAM = [
    ("ok", "ok"),
    ("http_402", "quota_exhausted"),
    ("insufficient_quota", "quota_exhausted"),
    ("http_429", "rate_limited"),
    ("http_500", "error"),
]

# 模組名 → [(kind, provider, model, api_base 是否帶 /v1)];api 成員一律帶 credential_ref
BUILTIN_HARNESS: dict[str, list[tuple[str, str, str, bool]]] = {
    "anthropic_api": [("api", "anthropic", "claude-sonnet-4-5", False)],
    "openai_api": [("api", "openai", "gpt-4o-mini", True)],
    "litellm": [("api", "deepseek", "deepseek-chat", True), ("local", "hosted_vllm", "local-model", True)],
}


def _ctx() -> AdapterContext:
    return AdapterContext(credentials=lambda ref: KEY, clock=FakeClock())


def _extra_paths() -> list[Path]:
    raw = os.environ.get("SPILLWAY_CONTRACT_EXTRA_PATHS", "")
    return [Path(p) for p in raw.split(os.pathsep) if p]


def _registry() -> AdapterRegistry:
    r = AdapterRegistry(_ctx())
    r.discover(extra_paths=_extra_paths())
    return r


MODULES = _registry().modules()


def _harness(name: str, fake_api):
    if name in BUILTIN_HARNESS:
        out = []
        for kind, provider, model, v1 in BUILTIN_HARNESS[name]:
            dep = Deployment(
                id=f"{name}-{kind}", model_name=f"{name}-{kind}", kind=kind, provider=provider,
                model=model, params={"api_base": fake_api.base_url + ("/v1" if v1 else "")},
                credential_ref="env:SPILLWAY_TEST_KEY" if kind == "api" else None,
            )
            out.append((dep, lambda mode, m=model: fake_api.set_mode(m, mode)))
        return out
    try:
        fakes = importlib.import_module(f"tests.fakes.{name}")
    except ImportError:
        fakes = None
    hook = getattr(fakes, "call_status_harness", None)
    if hook is None:
        hook = _external_harness(name)
    if hook is None:
        pytest.fail(f"模組 {name} 沒有 call 狀態的 harness:請提供 tests/fakes/{name}/ 的 "
                    "call_status_harness(fake_api),或在 SPILLWAY_CONTRACT_HARNESS_PATHS 的目錄放 "
                    f"{name}_harness.py(design 規則 3)")
    return hook(fake_api)


def _external_harness(name: str):
    """repo 外模組的 harness:`SPILLWAY_CONTRACT_HARNESS_PATHS`(os.pathsep 分隔)目錄下的
    `<name>_harness.py`,取其 `call_status_harness`(末端審查 M29)。找不到回 None。"""
    raw = os.environ.get("SPILLWAY_CONTRACT_HARNESS_PATHS", "")
    for d in (Path(p) for p in raw.split(os.pathsep) if p):
        path = d / f"{name}_harness.py"
        if not path.is_file():
            continue
        spec = importlib.util.spec_from_file_location(f"_spillway_contract_harness_{name}", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        hook = getattr(mod, "call_status_harness", None)
        if hook is not None:
            return hook
    return None


def test_modules_discovered():
    """守門:參數化不可因為沒有發現任何模組而全部 skip(波次 1–2 施工審議建議修)。"""
    assert MODULES
    assert {"anthropic_api", "openai_api", "litellm"} <= {m.name for m in MODULES}


@pytest.mark.parametrize("info", MODULES, ids=[m.name for m in MODULES])
@pytest.mark.parametrize(("mode", "expected"), UPSTREAM, ids=[u[0] for u in UPSTREAM])
async def test_call_status(info, mode, expected, fake_api):
    assert info.available and info.instance is not None, info.reason
    cases = _harness(info.name, fake_api)
    assert cases, f"模組 {info.name} 的 harness 沒有任何成員"
    req = ChatRequest(model="contract", messages=[{"role": "user", "content": "hi"}])
    for dep, set_mode in cases:
        set_mode(mode)
        result = await info.instance.call(dep, req, None)
        assert result.status == expected, (dep.kind, dep.provider, result.error)
        assert result.ok is (expected == "ok")
        if not result.ok:
            assert result.error and KEY not in result.error


async def test_external_harness_drives_five_statuses(tmp_path, monkeypatch):
    """M29:repo 外模組的 harness 由 SPILLWAY_CONTRACT_HARNESS_PATHS 載入,假模組依模式回傳五種狀態。"""
    from tests.contract.fakemods import write_module

    name = "extmod_m29"
    write_module(tmp_path / "mods", name, providers=("extm29",), harness_dir=tmp_path / "harness")
    assert not (tmp_path / "mods" / name / f"{name}_harness.py").exists()  # harness 在模組資料夾外
    monkeypatch.setenv("SPILLWAY_CONTRACT_HARNESS_PATHS",
                       os.pathsep.join([str(tmp_path / "nothing"), str(tmp_path / "harness")]))
    hook = _external_harness(name)
    assert hook is not None
    reg = AdapterRegistry(_ctx())
    reg.discover(extra_paths=[tmp_path / "mods"])
    info = next(m for m in reg.modules() if m.name == name)
    assert info.available, info.reason
    cases = hook(None)
    req = ChatRequest(model="contract", messages=[{"role": "user", "content": "hi"}])
    for mode, expected in UPSTREAM:
        for dep, set_mode in cases:
            set_mode(mode)
            result = await info.instance.call(dep, req, None)
            assert (result.status, result.ok) == (expected, expected == "ok")
    monkeypatch.setenv("SPILLWAY_CONTRACT_HARNESS_PATHS", str(tmp_path / "nothing"))
    assert _external_harness(name) is None

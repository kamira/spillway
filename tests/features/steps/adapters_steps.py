"""m2_adapters.feature 專用步驟(CHG-20261030-02 T15;AC-19、7.6.5、7.6.7)。

測試模組以 `tests.contract.fakemods.write_module` 寫在情境的暫存目錄(決策 15:不寫入 src/);
「啟動閘道」= 以 `create_app(extra_adapter_paths=[暫存目錄])` 建新 app(AC-19 的「重新啟動」)。
「合約測試對該模組通過」以子行程執行 `tests/contract/test_adapter_contract.py` 與
`tests/contract/test_call_status.py`(design 規則 3 的兩個檔案),經 `SPILLWAY_CONTRACT_EXTRA_PATHS`
把暫存目錄納入參數化、經 `SPILLWAY_CONTRACT_HARNESS_PATHS` 提供模組資料夾外的 call 狀態 harness,
並只選該模組的參數化項;斷言 call_status 的 5 組上游輸出全數通過。
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

from behave import step, use_step_matcher

from tests.contract.fakemods import write_module

use_step_matcher("re")

REPO_ROOT = Path(__file__).resolve().parents[3]
BUILTIN_ADAPTERS = REPO_ROOT / "src" / "spillway" / "adapters"


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def _mods_dir(context) -> Path:
    d = context.tmp / "adapter-modules"
    d.mkdir(exist_ok=True)
    return d


def _harness_dir(context) -> Path:
    # call 狀態 harness 放在模組資料夾外(spec 7.6.1 固定模組檔案清單;末端審查 M29)
    return context.tmp / "contract-harness"


def _adapters(gw) -> list[dict]:
    return gw.ok(gw.get("/api/adapters"), 200)["modules"]


def _module_for(gw, provider: str) -> dict:
    mods = [m for m in _adapters(gw) if provider in m["providers"]]
    assert len(mods) == 1, mods
    return mods[0]


@step(r'另外放入一個宣告 provider "([^"]+)" 的測試模組資料夾,未修改任何既有檔案')
def step_put_module(context, provider):
    before = _tree_digest(BUILTIN_ADAPTERS)
    path = write_module(_mods_dir(context), f"{provider}_module", providers=(provider,),
                        harness_dir=_harness_dir(context))
    context.module_dir = path
    assert not path.resolve().is_relative_to(REPO_ROOT)
    assert _tree_digest(BUILTIN_ADAPTERS) == before, "既有的 adapter 模組檔案被修改"


@step(r'另外放入一個宣告 provider "([^"]+)" 且 cardinality 為 single 的測試模組資料夾')
def step_put_single_module(context, provider):
    context.module_dir = write_module(_mods_dir(context), f"{provider}_module", providers=(provider,),
                                      cardinality="single")


@step(r'另外放入一個 __init__.py 無法 import 的測試模組資料夾,宣告 provider "([^"]+)"')
def step_put_broken_module(context, provider):
    context.module_dir = write_module(_mods_dir(context), f"{provider}_module", providers=(provider,),
                                      pre_import="import spillway_no_such_dependency_t15\n")


@step(r"啟動閘道")
def step_start(context):
    gw = context.gw
    try:
        gw.start(extra_paths=[_mods_dir(context)])
    except BaseException as exc:  # noqa: BLE001 — 由「閘道正常啟動」步驟判定
        gw.start_error = exc
        raise


@step(r"閘道正常啟動")
def step_started_ok(context):
    gw = context.gw
    assert gw.start_error is None and gw.started
    assert gw.get("/v1/models").status_code == 200
    builtin = {m["module"]: m for m in _adapters(gw) if m["builtin"]}
    assert {"anthropic_api", "openai_api", "litellm"} <= set(builtin)
    assert all(m["available"] for m in builtin.values()), builtin


@step(r'GET /api/adapters 列出 provider "([^"]+)"')
def step_adapters_lists(context, provider):
    m = _module_for(context.gw, provider)
    assert m["available"] is True and m["builtin"] is False, m
    assert m["module"] == context.module_dir.name, m
    assert m["kinds"] == ["api"] and m["cardinality"] == "multiple", m
    assert {x["id"] for x in m["models"]} >= {"fake-small", "fake-large"}, m


@step(r'GET /api/adapters 顯示 provider "([^"]+)" 不可用並附原因')
def step_adapters_unavailable(context, provider):
    m = _module_for(context.gw, provider)
    assert m["available"] is False and m["cardinality"] is None, m
    assert isinstance(m["unavailable_reason"], str) and m["unavailable_reason"].strip(), m


@step(r"合約測試對該模組通過")
def step_contract(context):
    name = context.module_dir.name
    env = {k: v for k, v in os.environ.items()
           if k not in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    env["SPILLWAY_CONTRACT_EXTRA_PATHS"] = str(context.module_dir.parent)
    env["SPILLWAY_CONTRACT_HARNESS_PATHS"] = str(_harness_dir(context))
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)])
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    contract = REPO_ROOT / "tests" / "contract"
    # design 規則 3:合約測試 = 形狀契約 + call 狀態,兩檔都跑(末端審查 M29)
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-v", "-p", "no:cacheprovider",
         str(contract / "test_adapter_contract.py"), str(contract / "test_call_status.py"), "-k", name],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=300)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "FAILED" not in out and " failed" not in out and "ERROR" not in out, out
    shape = re.findall(r"test_adapter_contract\.py::\S+ PASSED", out)
    # test_module_contract、test_effort_map_has_five_levels、test_models_efforts_only_five_levels
    assert len(shape) >= 3, out
    status = re.findall(r"test_call_status\.py::test_call_status\[([^\]]+)\] PASSED", out)
    modes = {"ok", "http_402", "insufficient_quota", "http_429", "http_500"}
    got = {p for ids in status for p in ids.split("-") if p in modes and name in ids}
    assert len(status) == 5 and got == modes, out


@step(r'以 provider "([^"]+)" 建立成員 "([^"]+)" 並建立 target 只含它的 route "([^"]+)"')
def step_member_and_route(context, provider, dep_id, route):
    gw = context.gw
    gw.ok(gw.create_member(gw.plain_body(dep_id, provider)), 201)
    gw.create_route(route, [[dep_id]])


@step(r'以 provider "([^"]+)" 建立成員 "([^"]+)"')
def step_member(context, provider, dep_id):
    gw = context.gw
    gw.create_member(gw.plain_body(dep_id, provider))


@step(r'第二次建立回應狀態為 (\d+) 且錯誤碼為 "([^"]+)"')
def step_second_create(context, status, code):
    gw = context.gw
    assert len(gw.created) >= 2
    assert gw.created[0].status_code == 201, gw.created[0].text
    second = gw.created[1]
    assert second.status_code == int(status), second.text
    body = second.json()
    assert set(body) == {"error"} and body["error"]["code"] == code, body
    assert body["error"]["message"], body


@step(r'資料庫中 provider "([^"]+)" 只有 (\d+) 個成員')
def step_db_count(context, provider, n):
    with context.gw.db() as conn:
        rows = conn.execute("SELECT id FROM deployments WHERE provider = ?", (provider,)).fetchall()
    assert len(rows) == int(n), [r[0] for r in rows]


@step(r'使用 provider "([^"]+)" 的成員於候選篩選的原因為 "([^"]+)"')
def step_unavailable_reason(context, provider, reason):
    gw = context.gw
    dep_id = f"{provider}-1"
    gw.ok(gw.create_member(gw.plain_body(dep_id, provider)), 201)
    gw.create_route(f"{provider}-route", [[dep_id]])
    plan = gw.dry_run(f"{provider}-route")
    excluded = {x["deployment"]: x["reason"] for x in plan["excluded"]}
    assert excluded.get(dep_id) == reason, plan
    assert plan["first"] is None, plan

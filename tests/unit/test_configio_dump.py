"""`spillway.configio.dump` 與匯出格式(CHG-20261102-02 T3;決策 5、8、18;契約清單「設定檔格式」)。

L1:
- 黃金檔:`seed_golden(conn)` 種入的資料庫匯出,逐位元組等於 `tests/unit/data/config_golden.yaml`
  (每欄都有一組非預設值;同 slot 兩成員帶 weight、內嵌 tag target、`type: none` 的 budget)。
- 鍵順序、排序、逐欄省略規則(`record_context: false`、`type: none`、`window: none`、`effort: none`
  不省略;`params`/`capabilities` 不遞迴剝除;slot 空洞重新編號;route tags 保留順序與重複值)。
- 兩次匯出逐位元組相同;不含 token 雜湊、冷卻、帳本、payload。
- 分層:`import spillway.configio` 之後 `"spillway.api" not in sys.modules`(子行程)。

`seed_golden(conn)` 供 T4 的往返測試重用(黃金檔 replace 匯入空 DB 後的 DB 全表快照對照):
slot 以 1 起算、position 以 0 起算(與 `PUT /api/routes/{name}/order` 的寫法一致)。
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from spillway import configio
from spillway.domain import Budget, Deployment
from spillway.store import db as store_db
from spillway.store import repo

GOLDEN = Path(__file__).parent / "data" / "config_golden.yaml"
REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN_HASH = "f" * 64
NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)


def seed_golden(conn) -> None:
    """黃金檔對應的資料庫內容(另含不該出現在匯出檔的 token 雜湊、冷卻、帳本、payload)。"""
    with store_db.transaction(conn):
        repo.set_setting(conn, "record_context_default", False)
        repo.set_setting(conn, "context_retention_days", 30)
        repo.set_setting(conn, "context_max_bytes", 1048576)
        repo.set_setting(conn, "access_token_sha256", TOKEN_HASH)
        repo.save_deployment(conn, Deployment(
            id="api-a", model_name="gpt-4o-mini", kind="api", provider="openai", model="gpt-4o-mini",
            params={"api_base": "http://127.0.0.1:9/v1", "timeout": 30},
            credential_ref="env:SPILLWAY_TEST_KEY",
            capabilities={"ctx": 128000, "efforts": ["low", "medium", "high"], "tools": True,
                          "vision": False},
            enabled=False, tags=("coding", "review"),
            budget=Budget(type="usd", limit_amount=30.0, window="monthly", reset_anchor=1)))
        repo.save_deployment(conn, Deployment(
            id="api-b", model_name="gpt-4.1-mini", kind="api", provider="openai",
            model="gpt-4.1-mini", credential_ref="keyring:spillway:api-b", tags=("cheap", "coding"),
            budget=Budget(type="credit_pool", limit_amount=100.0, window="weekly")))
        repo.save_deployment(conn, Deployment(
            id="bare", model_name="bare", kind="local", provider="hosted_vllm", model="local-model"))
        repo.save_deployment(conn, Deployment(
            id="claude-sub", model_name="sonnet", kind="cli", provider="claude-cli",
            model="claude-sonnet-4-5",
            params={"cwd": "/tmp/work", "extra_args": ["--verbose"], "nested": {"empty": {}}},
            tags=("coding",), budget=Budget(type="none", limit_amount=None, window="none")))
        repo.save_deployment(conn, Deployment(
            id="qwen-local", model_name="qwen-coder", kind="local", provider="ollama",
            model="qwen3-coder:30b", params={"api_base": "http://127.0.0.1:11434"},
            capabilities={"ctx": 32768, "efforts": []}, tags=("coding", "private-ok")))
        repo.save_group(conn, repo.Group("coding", {"all": ["coding"], "any": [], "none": []}))
        repo.save_group(conn, repo.Group("mixed", {"all": ["coding"], "any": ["review", "cheap"],
                                                   "none": ["private-ok"]}))
        repo.save_group(conn, repo.Group("中文群組", {"all": [], "any": [], "none": []}))
        repo.save_route(conn, repo.Route(
            name="review", target={"group": "coding"}, mode="task", tags=("daily", "alpha", "daily"),
            record_context=True, budget_precheck=True))
        repo.replace_route_targets(conn, "review", [
            repo.RouteTarget("claude-sub", slot=1, position=0, effort="high"),
            repo.RouteTarget("api-a", slot=2, position=0, effort="medium"),
            repo.RouteTarget("api-b", slot=2, position=1, effort="none", weight="high"),
            repo.RouteTarget("qwen-local", slot=3, position=0, weight="low"),
        ])
        repo.save_route(conn, repo.Route(
            name="quick", target={"tags": {"all": ["coding"], "any": [], "none": ["review"]}}))
        repo.save_route(conn, repo.Route(
            name="silent", target={"tags": {"all": [], "any": [], "none": []}},
            record_context=False))
        # 以下不可出現在匯出檔
        repo.mark_cooldown(conn, "api-a", NOW, "rate_limited")
        lid = repo.insert_ledger(conn, repo.LedgerRow(ts=NOW, status="ok", deployment_id="api-a",
                                                      error="ledger-only-text"))
        repo.insert_payload(conn, repo.Payload(ledger_id=lid, ts=NOW,
                                               request_json='"payload-only-text"', bytes=19))


@pytest.fixture
def conn(tmp_path: Path):
    c = store_db.connect(tmp_path / "dump.db")
    try:
        yield c
    finally:
        c.close()


# ---------------------------------------------------------------- 黃金檔與整體


def test_golden_bytes(conn):
    seed_golden(conn)
    assert configio.dump(conn).encode("utf-8") == GOLDEN.read_bytes()


def test_dump_is_stable(conn):
    seed_golden(conn)
    first = configio.dump(conn)
    assert configio.dump(conn) == first
    assert first.endswith("\n") and not first.endswith("\n\n")


def test_golden_excludes_runtime_state(conn):
    seed_golden(conn)
    text = configio.dump(conn)
    for needle in (TOKEN_HASH, "access_token", "ledger-only-text", "payload-only-text",
                   "rate_limited", "exhausted", "cooldown", "8787", "port"):
        assert needle not in text, needle


def test_empty_database():
    # 不需連線:空資料的文件同樣一律輸出五個頂層鍵
    doc = configio.build_document(settings={}, deployments=[], groups=[], routes=[],
                                  route_targets={})
    assert configio.render(doc) == (
        "version: 1\nsettings: {}\ndeployments: []\ngroups: {}\nroutes: {}\n")


def test_empty_database_dump(conn):
    assert configio.dump(conn) == "version: 1\nsettings: {}\ndeployments: []\ngroups: {}\nroutes: {}\n"
    assert not conn.in_transaction   # 自行開的讀交易已結束


def test_dump_reuses_open_transaction(conn):
    seed_golden(conn)
    with store_db.transaction(conn):
        repo.save_group(conn, repo.Group("zz-in-tx", {"all": ["x"]}))
        text = configio.dump(conn)
        assert conn.in_transaction
    assert "zz-in-tx" in text   # 看得到同一交易內尚未提交的寫入


def test_top_level_order_and_version(conn):
    seed_golden(conn)
    doc = yaml.safe_load(configio.dump(conn))
    assert list(doc) == list(configio.TOP_KEYS)
    assert doc["version"] == configio.FORMAT_VERSION == 1


# ---------------------------------------------------------------- 逐欄規則


def test_deployment_key_order_and_sorting(conn):
    seed_golden(conn)
    doc = yaml.safe_load(configio.dump(conn))
    ids = [d["id"] for d in doc["deployments"]]
    assert ids == sorted(ids)
    full = doc["deployments"][0]
    assert list(full) == list(configio.DEPLOYMENT_KEYS)          # api-a 每欄都有
    assert list(full["budget"]) == list(configio.BUDGET_KEYS)


def test_deployment_omissions():
    dep = Deployment(id="d", model_name="m", kind="api", provider="p", model="x")
    assert configio.deployment_doc(dep) == {"id": "d", "model_name": "m", "kind": "api",
                                            "provider": "p", "model": "x"}
    dep2 = Deployment(id="d", model_name="m", kind="api", provider="p", model="x", enabled=False)
    assert configio.deployment_doc(dep2)["enabled"] is False


def test_params_and_capabilities_not_stripped():
    dep = Deployment(id="d", model_name="m", kind="api", provider="p", model="x",
                     params={"a": {}, "b": [], "c": None, "d": False},
                     capabilities={"efforts": [], "tools": False})
    out = configio.deployment_doc(dep)
    assert out["params"] == {"a": {}, "b": [], "c": None, "d": False}
    assert out["capabilities"] == {"efforts": [], "tools": False}


def test_budget_rules():
    assert configio.budget_doc(Budget(type="none", limit_amount=None, window="none")) == {
        "type": "none", "window": "none"}
    assert configio.budget_doc(Budget(type="usd", limit_amount=0.0, window="daily",
                                      reset_anchor=None)) == {
        "type": "usd", "limit_amount": 0.0, "window": "daily"}
    assert list(configio.budget_doc(Budget(type="usd", limit_amount=5.0, window="monthly",
                                           reset_anchor=28))) == list(configio.BUDGET_KEYS)


def test_selector_and_target_rules():
    assert configio.selector_doc({"all": [], "any": [], "none": []}) == {}
    assert configio.selector_doc({"none": ["b", "a", "b"], "all": ["z"]}) == {
        "all": ["z"], "none": ["b", "a", "b"]}
    assert configio.target_doc({"group": "g"}) == {"group": "g"}
    assert configio.target_doc({"tags": {"all": [], "any": [], "none": []}}) == {"tags": {}}


def test_route_rules():
    r = repo.Route(name="r", target={"group": "g"})
    assert configio.route_doc(r) == {"target": {"group": "g"}}
    r = repo.Route(name="r", target={"group": "g"}, record_context=False)
    assert configio.route_doc(r) == {"target": {"group": "g"}, "record_context": False}
    r = repo.Route(name="r", target={"group": "g"}, mode="task", tags=("b", "a", "b"),
                   record_context=True, budget_precheck=True)
    out = configio.route_doc(r, [repo.RouteTarget("x", 1, 0)])
    assert list(out) == list(configio.ROUTE_KEYS)
    assert out["tags"] == ["b", "a", "b"]
    assert out["targets"] == [[{"deployment": "x"}]]


def test_target_entry_rules():
    rows = [
        repo.RouteTarget("a", slot=2, position=0, effort="none"),
        repo.RouteTarget("b", slot=2, position=1, effort=None, weight="high"),
        repo.RouteTarget("c", slot=7, position=0, effort="max", weight="medium"),
        repo.RouteTarget("d", slot=9, position=3, weight="low"),
    ]
    assert configio.targets_doc(rows) == [
        [{"deployment": "a", "effort": "none"}, {"deployment": "b", "weight": "high"}],
        [{"deployment": "c", "effort": "max"}],
        [{"deployment": "d", "weight": "low"}],
    ]
    entry = configio.targets_doc([repo.RouteTarget("a", 1, 0, effort="low", weight="low")])[0][0]
    assert list(entry) == list(configio.TARGET_ENTRY_KEYS)


def test_slot_holes_renumbered_in_dump(conn):
    repo.save_deployment(conn, Deployment(id="a", model_name="a", kind="local", provider="p",
                                          model="m"))
    repo.save_deployment(conn, Deployment(id="b", model_name="b", kind="local", provider="p",
                                          model="m"))
    repo.save_route(conn, repo.Route(name="r", target={"group": "nope"}))   # 目標群組不存在也照出
    repo.replace_route_targets(conn, "r", [repo.RouteTarget("b", 4, 0),
                                           repo.RouteTarget("a", 10, 5)])
    doc = yaml.safe_load(configio.dump(conn))
    assert doc["routes"]["r"]["targets"] == [[{"deployment": "b"}], [{"deployment": "a"}]]


def test_settings_only_non_default_in_fixed_order(conn):
    assert configio.settings_doc({}) == {}
    assert configio.settings_doc(dict(repo.SETTING_DEFAULTS)) == {}
    repo.set_setting(conn, "context_max_bytes", 2048)
    repo.set_setting(conn, "record_context_default", False)
    repo.set_setting(conn, "context_retention_days", 7)   # 等於預設:不輸出
    repo.set_setting(conn, "access_token_sha256", TOKEN_HASH)
    doc = yaml.safe_load(configio.dump(conn))
    assert doc["settings"] == {"record_context_default": False, "context_max_bytes": 2048}
    assert list(doc["settings"]) == ["record_context_default", "context_max_bytes"]
    assert list(configio.SETTING_KEYS) == list(repo.SETTING_DEFAULTS)


def test_groups_and_routes_sorted_by_name(conn):
    for name in ("b", "a", "C", "中"):
        repo.save_group(conn, repo.Group(name, {"all": ["t"]}))
        repo.save_route(conn, repo.Route(name=name, target={"group": name}))
    doc = yaml.safe_load(configio.dump(conn))
    assert list(doc["groups"]) == sorted(["b", "a", "C", "中"])
    assert list(doc["routes"]) == sorted(["b", "a", "C", "中"])


def test_ambiguous_strings_round_trip_as_strings():
    # 看起來像 bool、數字、日期、null 的字串必須加引號;不產生 anchor/alias
    shared = ["x"]
    doc = {"s": ["on", "yes", "true", "1", "2026-11-02", "null", "~", "", "a: b", "#c"],
           "p": shared, "q": shared}
    text = configio.render(doc)
    assert "&" not in text and "*" not in text
    assert yaml.safe_load(text) == doc


def test_unicode_not_escaped():
    text = configio.render({"groups": {"中文群組": {}}})
    assert "中文群組" in text


# ---------------------------------------------------------------- 分層


def test_configio_does_not_import_api():
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    code = ("import sys, spillway.configio; "
            "bad = sorted(m for m in sys.modules if m == 'spillway.api' or m.startswith('spillway.api.')); "
            "print(','.join(bad)); sys.exit(1 if bad else 0)")
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr

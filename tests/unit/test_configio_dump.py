"""`spillway.configio.dump` 與匯出格式(CHG-20261102-02 T3;決策 5、8、18;契約清單「設定檔格式」)。

L1:
- 黃金檔:`seed_golden(conn)` 種入的資料庫匯出,逐位元組等於 `tests/unit/data/config_golden.yaml`
  (每欄都有一組非預設值;同 slot 兩成員帶 weight、內嵌 tag target、`type: none` 的 budget)。
- 鍵順序、排序、逐欄省略規則(`record_context: false`、`type: none`、`window: none`、`effort: none`
  不省略;`params`/`capabilities` 不遞迴剝除;slot 空洞重新編號;route tags 保留順序與重複值)。
- 兩次匯出逐位元組相同;不含 token 雜湊、冷卻、帳本、payload。
- 分層:`import spillway.configio` 之後 `"spillway.api" not in sys.modules`(子行程)。

黃金檔與 `seed_golden` 在 `tests/helpers/config_golden.py`(T4 的往返測試共用)。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from spillway import configio
from spillway.domain import Budget, Deployment
from spillway.store import db as store_db
from spillway.store import repo
from tests.helpers.config_golden import GOLDEN, TOKEN_HASH, seed_golden

REPO_ROOT = Path(__file__).resolve().parents[2]


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
    assert configio._deployment_doc(dep) == {"id": "d", "model_name": "m", "kind": "api",
                                            "provider": "p", "model": "x"}
    dep2 = Deployment(id="d", model_name="m", kind="api", provider="p", model="x", enabled=False)
    assert configio._deployment_doc(dep2)["enabled"] is False


def test_params_and_capabilities_not_stripped():
    dep = Deployment(id="d", model_name="m", kind="api", provider="p", model="x",
                     params={"a": {}, "b": [], "c": None, "d": False},
                     capabilities={"efforts": [], "tools": False})
    out = configio._deployment_doc(dep)
    assert out["params"] == {"a": {}, "b": [], "c": None, "d": False}
    assert out["capabilities"] == {"efforts": [], "tools": False}


def test_budget_rules():
    assert configio._budget_doc(Budget(type="none", limit_amount=None, window="none")) == {
        "type": "none", "window": "none"}
    assert configio._budget_doc(Budget(type="usd", limit_amount=0.0, window="daily",
                                      reset_anchor=None)) == {
        "type": "usd", "limit_amount": 0.0, "window": "daily"}
    assert list(configio._budget_doc(Budget(type="usd", limit_amount=5.0, window="monthly",
                                           reset_anchor=28))) == list(configio.BUDGET_KEYS)


def test_selector_and_target_rules():
    assert configio._selector_doc({"all": [], "any": [], "none": []}) == {}
    assert configio._selector_doc({"none": ["b", "a", "b"], "all": ["z"]}) == {
        "all": ["z"], "none": ["b", "a", "b"]}
    assert configio._target_doc({"group": "g"}) == {"group": "g"}
    assert configio._target_doc({"tags": {"all": [], "any": [], "none": []}}) == {"tags": {}}


def test_route_rules():
    r = repo.Route(name="r", target={"group": "g"})
    assert configio._route_doc(r) == {"target": {"group": "g"}}
    r = repo.Route(name="r", target={"group": "g"}, record_context=False)
    assert configio._route_doc(r) == {"target": {"group": "g"}, "record_context": False}
    r = repo.Route(name="r", target={"group": "g"}, mode="task", tags=("b", "a", "b"),
                   record_context=True, budget_precheck=True)
    out = configio._route_doc(r, [repo.RouteTarget("x", 1, 0)])
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
    assert configio._targets_doc(rows) == [
        [{"deployment": "a", "effort": "none"}, {"deployment": "b", "weight": "high"}],
        [{"deployment": "c", "effort": "max"}],
        [{"deployment": "d", "weight": "low"}],
    ]
    entry = configio._targets_doc([repo.RouteTarget("a", 1, 0, effort="low", weight="low")])[0][0]
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
    assert configio._settings_doc({}) == {}
    assert configio._settings_doc(dict(repo.SETTING_DEFAULTS)) == {}
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


@pytest.mark.parametrize("ch", ["\x85", "\u2028", "\u2029"])
def test_line_break_chars_double_quoted_and_round_trip(ch):
    doc = {"groups": {f"g{ch}x": {"all": [f"a{ch}b", ch]}}, "s": f"{ch}lead", "t": f"trail{ch}"}
    text = configio.render(doc)
    assert ch not in text                     # 以 \N、\L、\P 跳脫,不原樣寫出
    assert yaml.safe_load(text) == doc


def test_line_break_chars_in_golden_db_round_trip(conn):
    for ch in ("\x85", "\u2028", "\u2029"):
        repo.save_group(conn, repo.Group(f"g{ch}", {"all": [f"t{ch}"]}))
    doc = yaml.safe_load(configio.dump(conn))
    assert doc["groups"] == {f"g{ch}": {"all": [f"t{ch}"]} for ch in ("\x85", "\u2028", "\u2029")}


def test_utc_stamp_filename():
    from datetime import datetime, timedelta, timezone

    fmt = "spillway-config-%Y%m%dT%H%M%SZ.yaml"
    tz = timezone(timedelta(hours=8))
    assert configio.utc_stamp_filename(datetime(2026, 11, 2, 9, 5, 7, tzinfo=tz), fmt) == (
        "spillway-config-20261102T010507Z.yaml")
    assert configio.utc_stamp_filename(datetime(2026, 1, 1, tzinfo=timezone.utc),
                                       "config-%Y%m%dT%H%M%SZ.yaml") == "config-20260101T000000Z.yaml"


def test_setting_types_cover_defaults():
    assert set(configio.SETTING_TYPES) == set(configio.SETTING_KEYS) == set(repo.SETTING_DEFAULTS)


def test_public_surface():
    assert set(configio.__all__) == {
        "FORMAT_VERSION", "TOP_KEYS", "SETTING_KEYS", "SETTING_TYPES", "DEPLOYMENT_KEYS",
        "BUDGET_KEYS", "SELECTOR_KEYS", "ROUTE_KEYS", "TARGET_ENTRY_KEYS", "DEFAULT_MODE",
        "DEFAULT_WEIGHT", "DUMP_KWARGS", "dump", "render", "build_document", "utc_stamp_filename"}
    assert all(hasattr(configio, n) for n in configio.__all__)


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

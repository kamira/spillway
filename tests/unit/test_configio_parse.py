"""設定檔解析與套用(CHG-20261102-02 T4;決策 6、7、8、18、20;契約清單「load_yaml/apply、parse」)。

L1(本檔,不經 HTTP):
- 嚴格 loader:anchor/alias、alias 炸彈、自我參照、`"["*50000`、深度、重複鍵、非字串鍵、`!!binary`、
  `!!set`、日期值、`on`/`yes`、`version: 2`、非 mapping、語法錯誤不回顯原始行(含 `AIza…` 假金鑰)。
- `parse`:未知鍵、型別錯誤、憑證鍵名、不合法 signals、群組名規則、route 名、`targets` 重複成員、
  重複成員 id 的路徑格式;多個錯誤一次回、上限 20。
- `apply`:`privileged` 欄位集、dry_run 回滾、targets 參照、target.group、single、備份檔規則。
"""
from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest
import yaml

from spillway import configio
from spillway.api import ApiError
from spillway.api.admin import config_file
from spillway.store import db as store_db
from spillway.store import repo
from tests.helpers.config_golden import GOLDEN, NOW, seed_golden

FAKE_AIZA = "AIzaSyD-fake-0000-test-key-q9z7"
FAKE_SK = "sk-test-0000"


def _single(provider: str, kind: str) -> str:
    return "single" if provider == "claude-cli" else "multiple"


@pytest.fixture
def conn(tmp_path: Path):
    c = store_db.connect(tmp_path / "parse.db")
    try:
        yield c
    finally:
        c.close()


def _apply(conn, text: str, mode: str = "replace", *, tmp_path: Path, dry_run: bool = False):
    return configio.apply(conn, config_file.parse(text), mode, cardinality_of=_single,
                          backup_dir=tmp_path / "backups", now=NOW, dry_run=dry_run)


def _doc(**top) -> str:
    return yaml.safe_dump({"version": 1, **top}, sort_keys=False, allow_unicode=True)


def _parse_error(text: str) -> str:
    with pytest.raises(ApiError) as ei:
        config_file.parse(text)
    assert ei.value.status == 422 and ei.value.code == "validation_error"
    return ei.value.message


def _member(dep_id: str, **kw) -> dict:
    body = {"id": dep_id, "model_name": dep_id, "kind": "api", "provider": "openai", "model": "gpt"}
    body.update(kw)
    return body


# ================================================================ 嚴格 loader


@pytest.mark.parametrize("text,needle", [
    ("version: 1\na: &x 1\nb: *x\n", "anchor/alias"),
    ("version: 1\na: &x [*x]\n", "anchor/alias"),
    ("version: 1\nx: !!binary aGk=\n", "tag"),
    ("version: 1\nx: !!set {a: null}\n", "tag"),
    ("version: 1\nx: !custom 1\n", "tag"),
    ("version: 1\nversion: 1\n", "重複"),
    ("version: 1\n1: a\n", "字串"),
    ("version: 1\n? [a]\n: b\n", "字串"),
    ("version: 1\ntrue: x\n", "字串"),
    ("version: 2\n", "version"),
    ("version: true\n", "version"),
    ("version: '1'\n", "version"),
    ("- a\n", "mapping"),
    ("", "mapping"),
    ("version: 1\n---\nversion: 1\n", "yaml"),
    ("version: 1\nx: \x01\n", "不允許的字元"),
    ("version: 1\n\x00", "不允許的字元"),
    ('version: 1\nx: "a\\ud800"\n', "代理字元"),
    ('version: 1\nroutes:\n  "\\udc00": {}\n', "代理字元"),
])
def test_loader_rejects(text, needle):
    with pytest.raises(configio.ConfigSyntaxError) as ei:
        configio.load_yaml(text)
    assert needle in str(ei.value)


def test_duplicate_key_path():
    with pytest.raises(configio.ConfigSyntaxError) as ei:
        configio.load_yaml("version: 1\nroutes:\n  a.b:\n    x: 1\n    x: 2\n")
    assert ei.value.path == 'routes["a.b"].x'


def test_alias_bomb_fast():
    lines = ['a: &a ["x","x","x","x","x","x","x","x","x"]']
    for i in range(1, 9):
        prev, cur = chr(ord("a") + i - 1), chr(ord("a") + i)
        lines.append(f"{cur}: &{cur} [{','.join(['*' + prev] * 9)}]")
    t0 = time.monotonic()
    with pytest.raises(configio.ConfigSyntaxError):
        configio.load_yaml("version: 1\n" + "\n".join(lines) + "\n")
    assert time.monotonic() - t0 < 1


@pytest.mark.parametrize("text", ["[" * 50000, "version: 1\nx: " + "[" * 50000, "{a: " * 5000])
def test_deep_nesting_fast(text):
    t0 = time.monotonic()
    with pytest.raises(configio.ConfigSyntaxError):
        configio.load_yaml(text)
    assert time.monotonic() - t0 < 1


def test_depth_limit_boundary():
    ok = "version: 1\nx: " + "[" * (configio.MAX_DEPTH - 1) + "]" * (configio.MAX_DEPTH - 1) + "\n"
    configio.load_yaml(ok)
    bad = "version: 1\nx: " + "[" * configio.MAX_DEPTH + "]" * configio.MAX_DEPTH + "\n"
    with pytest.raises(configio.ConfigSyntaxError):
        configio.load_yaml(bad)


def test_scalars_kept_as_strings():
    data = configio.load_yaml(
        "version: 1\nd: 2026-01-01\nt: 2026-01-01T00:00:00Z\no: on\ny: yes\nn: no\nT: TRUE\nf: False\n"
        "m: <<\n")
    assert data == {"version": 1, "d": "2026-01-01", "t": "2026-01-01T00:00:00Z", "o": "on",
                    "y": "yes", "n": "no", "T": True, "f": False, "m": "<<"}


def test_syntax_error_does_not_echo_line():
    text = f'version: 1\nkey: "{FAKE_AIZA}\n  bad: [\n'
    with pytest.raises(configio.ConfigSyntaxError) as ei:
        configio.load_yaml(text)
    assert ei.value.path == "yaml"
    assert ei.value.reason.startswith("line ")
    assert FAKE_AIZA not in str(ei.value)
    assert "AIza" not in _parse_error(text)


def test_golden_loads_to_plain_data():
    data = configio.load_yaml(GOLDEN.read_text(encoding="utf-8"))
    assert data == yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))


# ================================================================ parse:檔案模型


def test_format_path():
    assert configio.format_path(("deployments", 1, "kind")) == "deployments[1].kind"
    assert configio.format_path(("routes", "a.b", "targets", 0, 1, "deployment")) == (
        'routes["a.b"].targets[0][1].deployment')
    assert configio.format_path(("groups", "中文")) == 'groups["中文"]'


@pytest.mark.parametrize("top,path", [
    ({"deployments": [_member("a"), _member("b", kind="bogus")]}, "deployments[1].kind"),
    ({"deployments": [_member("a", color="red")]}, "deployments[0].color"),
    ({"bogus": 1}, "bogus"),
    ({"settings": {"context_retention_days": "7"}}, "settings.context_retention_days"),
    ({"settings": {"unknown": 1}}, "settings.unknown"),
    ({"deployments": [_member("a", params={"api_key": "x"})]}, "deployments[0].params"),
    ({"deployments": [_member("a", params={"signals": {"bogus": []}})]}, "deployments[0].params"),
    ({"deployments": [_member("a", capabilities={"secret": "x"})]}, "deployments[0].capabilities"),
    ({"deployments": [_member("a", credential_ref="plain")]}, "deployments[0].credential_ref"),
    ({"deployments": [_member("a", budget={"type": "usd", "reset_anchor": 40})]},
     "deployments[0].budget.reset_anchor"),
    ({"groups": {"g": {"all": ["x"], "some": []}}}, "groups.g.some"),
    ({"routes": {"a.b": {"target": {"group": "g"}, "targets": [[{"deployment": 1}]]}}},
     'routes["a.b"].targets[0][0].deployment'),
    ({"routes": {"r": {"target": {"group": "g"}, "targets": [[]]}}}, "routes.r.targets[0]"),
    ({"routes": {"r": {"target": {"bogus": 1}}}}, "routes.r.target"),
    ({"routes": {"r": {"target": {"group": "g"}, "name": "other"}}}, "routes.r.name"),
    ({"routes": {"r:x": {"target": {"group": "g"}}}}, 'routes["r:x"]'),
    ({"deployments": [_member("a"), _member("a")]}, "deployments[1].id"),
    ({"deployments": [{k: v for k, v in _member("a").items() if k != "id"}]}, "deployments[0].id"),
])
def test_parse_paths(top, path):
    msg = _parse_error(_doc(**top))
    assert msg.startswith(f"{path}: ") or f"; {path}: " in msg, msg


@pytest.mark.parametrize("name", [".", "..", "all", "a b", "x/y", "\u202eabc", "a\u2028b", "\ue000",
                                  "e\u0301", "a" * 201])
def test_group_name_rules(name):
    msg = _parse_error(_doc(groups={name: {}}))
    assert "groups" in msg


def test_group_surrogate_rejected():
    # 孤立代理字元無法以 UTF-8 送出;以 JSON 跳脫寫進 YAML 雙引號字串
    msg = _parse_error('version: 1\ngroups:\n  "\\ud800": {}\n')
    assert "groups" in msg


@pytest.mark.parametrize("name", ["coding", "中文群組", "a.b", "v1-2"])
def test_group_name_ok(name):
    plan = config_file.parse(_doc(groups={name: {"all": ["x"]}}))
    assert plan.groups == (repo.Group(name, {"all": ["x"], "any": [], "none": []}),)


def test_targets_duplicate_member():
    msg = _parse_error(_doc(routes={"r": {"target": {"group": "g"}, "targets": [
        [{"deployment": "a"}], [{"deployment": "a"}]]}}))
    assert "routes.r.targets" in msg and "同一成員出現多次" in msg


def test_all_errors_capped_at_20():
    deps = [_member(f"d{i}", kind="bogus") for i in range(30)]
    msg = _parse_error(_doc(deployments=deps))
    parts = msg.split("; ")
    assert len(parts) == config_file.MAX_ERRORS
    assert parts[0].startswith("deployments[0].kind: ")
    assert parts[1].startswith("deployments[1].kind: ")


def test_secret_value_not_echoed():
    for params in ({"note": FAKE_SK}, {"nested": {"password": FAKE_AIZA}}):
        msg = _parse_error(_doc(deployments=[_member("a", params=params)]))
        assert FAKE_SK not in msg and FAKE_AIZA not in msg


def test_plan_shape():
    plan = config_file.parse(GOLDEN.read_text(encoding="utf-8"))
    assert [d.id for d in plan.deployments] == ["api-a", "api-b", "bare", "claude-sub", "qwen-local"]
    review = dict((r.name, t) for r, t in plan.routes)["review"]
    assert [(t.deployment_id, t.slot, t.position) for t in review] == [
        ("claude-sub", 1, 0), ("api-a", 2, 0), ("api-b", 2, 1), ("qwen-local", 3, 0)]
    assert dict((r.name, t) for r, t in plan.routes)["quick"] is None
    assert plan.settings == {"record_context_default": False, "context_retention_days": 30,
                             "context_max_bytes": 1048576}


def test_missing_sections_default_empty():
    plan = config_file.parse("version: 1\n")
    assert plan == configio.ConfigPlan()


# ================================================================ apply:privileged(決策 20)


BASE = [
    _member("api-x", params={"api_base": "http://127.0.0.1:1/v1", "timeout": 10},
            credential_ref="env:SPILLWAY_TEST_KEY", tags=["t"]),
    {"id": "cli-x", "model_name": "c", "kind": "cli", "provider": "codex-cli", "model": "m",
     "params": {"extra_args": ["-a"], "effort_args": {"high": ["-h"]}, "cwd": "/tmp/a"}},
    _member("plain"),
]


def _with(dep_id: str, **changes) -> list[dict]:
    out = []
    for d in BASE:
        d = {**d, "params": dict(d.get("params", {}))}
        if d["id"] == dep_id:
            for k, v in changes.items():
                if k.startswith("params."):
                    d["params"][k[len("params."):]] = v
                else:
                    d[k] = v
        out.append(d)
    return out


@pytest.mark.parametrize("dep_id,changes,expected", [
    ("api-x", {"params.api_base": "http://127.0.0.1:2/v1"}, ["api-x"]),
    ("api-x", {"params.base_url": "http://evil.example"}, ["api-x"]),
    ("api-x", {"params.proxy_url": "http://evil.example"}, ["api-x"]),
    ("api-x", {"params.endpoint": "http://evil.example"}, ["api-x"]),
    ("api-x", {"credential_ref": "env:OTHER_VAR"}, ["api-x"]),
    ("cli-x", {"params.extra_args": ["-b"]}, ["cli-x"]),
    ("cli-x", {"params.effort_args": {"high": ["-x"]}}, ["cli-x"]),
    ("cli-x", {"params.cwd": "/"}, ["cli-x"]),
    ("api-x", {"tags": ["other"]}, []),
    ("api-x", {"params.timeout": 99}, []),
    ("plain", {"model": "gpt-5"}, []),
])
def test_privileged_changes(conn, tmp_path, dep_id, changes, expected):
    _apply(conn, _doc(deployments=BASE), tmp_path=tmp_path)
    r = _apply(conn, _doc(deployments=_with(dep_id, **changes)), "merge", tmp_path=tmp_path)
    assert list(r.privileged) == expected


def test_privileged_new_and_deleted(conn, tmp_path):
    first = _apply(conn, _doc(deployments=BASE), tmp_path=tmp_path)
    assert first.privileged == ("api-x", "cli-x")          # 新增帶 credential_ref / api_base / CLI 參數
    r = _apply(conn, _doc(deployments=[BASE[2]]), tmp_path=tmp_path)   # 刪掉 api-x、cli-x
    assert r.privileged == ()
    assert r.deleted["deployments"] == 2


def test_privileged_values_not_in_result(conn, tmp_path):
    r = _apply(conn, _doc(deployments=BASE), tmp_path=tmp_path)
    text = repr(r.privileged)
    for needle in ("SPILLWAY_TEST_KEY", "127.0.0.1", "/tmp/a", "-a"):
        assert needle not in text


# ================================================================ apply:交易與檢查


def _table_snapshot(conn) -> list[str]:
    return list(conn.iterdump())


def test_dry_run_rolls_back(conn, tmp_path):
    seed_golden(conn)
    before = _table_snapshot(conn)
    text = _doc(deployments=[BASE[2]])
    r = _apply(conn, text, tmp_path=tmp_path, dry_run=True)
    assert r.dry_run is True and r.backup is None
    assert _table_snapshot(conn) == before
    assert not (tmp_path / "backups").exists()
    real = _apply(conn, text, tmp_path=tmp_path)
    assert (r.created, r.updated, r.deleted, r.privileged) == (
        real.created, real.updated, real.deleted, real.privileged)
    assert real.backup is not None


def test_targets_unknown_member(conn, tmp_path):
    seed_golden(conn)
    before = _table_snapshot(conn)
    text = _doc(routes={"r": {"target": {"tags": {}}, "targets": [[{"deployment": "ghost"}]]}})
    for mode in ("merge", "replace"):
        with pytest.raises(configio.ImportConflict) as ei:
            _apply(conn, text, mode, tmp_path=tmp_path)
        assert ei.value.status == 422
        assert ei.value.path == "routes.r.targets[0][0].deployment"
    assert _table_snapshot(conn) == before
    assert not (tmp_path / "backups").exists()   # 參照檢查在備份之前


def test_replace_target_member_must_be_in_file(conn, tmp_path):
    seed_golden(conn)   # api-a 在資料庫,但 replace 的檔案沒有 → 不存在於最終狀態
    text = _doc(routes={"r": {"target": {"tags": {}}, "targets": [[{"deployment": "api-a"}]]}})
    with pytest.raises(configio.ImportConflict):
        _apply(conn, text, "replace", tmp_path=tmp_path)
    r = _apply(conn, text, "merge", tmp_path=tmp_path)
    assert r.created["routes"] == 1


def test_target_group_must_exist(conn, tmp_path):
    with pytest.raises(configio.ImportConflict) as ei:
        _apply(conn, _doc(routes={"r.x": {"target": {"group": "ghost"}}}), tmp_path=tmp_path)
    assert ei.value.status == 422 and ei.value.path == 'routes["r.x"].target.group'
    r = _apply(conn, _doc(groups={"ghost": {}}, routes={"r.x": {"target": {"group": "ghost"}}}),
               tmp_path=tmp_path)
    assert r.created == {"deployments": 0, "groups": 1, "routes": 1}


def test_replace_cannot_delete_group_still_referenced_by_kept_route(conn, tmp_path):
    seed_golden(conn)
    doc = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    del doc["groups"]["coding"]
    with pytest.raises(configio.ImportConflict) as ei:
        _apply(conn, yaml.safe_dump(doc, allow_unicode=True), tmp_path=tmp_path)
    assert ei.value.path == "routes.review.target.group"


def _cli(dep_id: str) -> dict:
    return {"id": dep_id, "model_name": dep_id, "kind": "cli", "provider": "claude-cli", "model": "m"}


def test_single_two_in_file(conn, tmp_path):
    with pytest.raises(configio.ImportConflict) as ei:
        _apply(conn, _doc(deployments=[_cli("c1"), _cli("c2")]), "merge", tmp_path=tmp_path)
    assert ei.value.status == 409 and ei.value.code == "provider_single"
    assert "c1" in ei.value.message and "c2" in ei.value.message


def test_single_merge_with_existing(conn, tmp_path):
    _apply(conn, _doc(deployments=[_cli("c1"), _member("other")]), tmp_path=tmp_path)
    before = _table_snapshot(conn)
    changed = {**_member("other"), "kind": "cli", "provider": "claude-cli"}
    with pytest.raises(configio.ImportConflict) as ei:
        _apply(conn, _doc(deployments=[changed]), "merge", tmp_path=tmp_path)
    assert ei.value.code == "provider_single"
    assert _table_snapshot(conn) == before
    # replace 把 c1 換成 c2:最終只有 1 個,可以
    r = _apply(conn, _doc(deployments=[_cli("c2")]), "replace", tmp_path=tmp_path)
    assert r.created["deployments"] == 1 and r.deleted["deployments"] == 2


def test_invalid_mode(conn, tmp_path):
    with pytest.raises(ValueError):
        configio.apply(conn, configio.ConfigPlan(), "upsert", cardinality_of=_single,
                       backup_dir=tmp_path, now=NOW)


# ================================================================ 備份(決策 7)


def test_backup_file_rules(conn, tmp_path):
    seed_golden(conn)
    expected = configio.dump(conn)
    r = _apply(conn, "version: 1\n", tmp_path=tmp_path)
    backups = tmp_path / "backups"
    assert r.backup == "config-20261102T120000Z.yaml"
    path = backups / r.backup
    assert path.read_text(encoding="utf-8") == expected
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(backups.stat().st_mode) == 0o700
    # 同一秒再匯入:不覆寫,改用 -2、-3
    r2 = _apply(conn, "version: 1\n", tmp_path=tmp_path)
    r3 = _apply(conn, "version: 1\n", tmp_path=tmp_path)
    assert (r2.backup, r3.backup) == ("config-20261102T120000Z-2.yaml", "config-20261102T120000Z-3.yaml")
    assert path.read_text(encoding="utf-8") == expected


def test_backup_preexisting_not_overwritten(conn, tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "config-20261102T120000Z.yaml").write_text("keep", encoding="utf-8")
    r = _apply(conn, "version: 1\n", tmp_path=tmp_path)
    assert r.backup == "config-20261102T120000Z-2.yaml"
    assert (backups / "config-20261102T120000Z.yaml").read_text(encoding="utf-8") == "keep"


def test_backup_dir_symlink_rejected(conn, tmp_path):
    seed_golden(conn)
    before = _table_snapshot(conn)
    real = tmp_path / "elsewhere"
    real.mkdir()
    os.symlink(real, tmp_path / "backups")
    with pytest.raises(configio.ImportConflict) as ei:
        _apply(conn, "version: 1\n", tmp_path=tmp_path)
    assert ei.value.status == 500 and ei.value.code == "internal_error"
    assert _table_snapshot(conn) == before
    assert list(real.iterdir()) == []


def test_backup_dir_is_file_rejected(conn, tmp_path):
    (tmp_path / "backups").write_text("x", encoding="utf-8")
    with pytest.raises(configio.ImportConflict) as ei:
        _apply(conn, "version: 1\n", tmp_path=tmp_path)
    assert ei.value.status == 500


def test_backup_removed_when_later_check_fails(conn, tmp_path):
    seed_golden(conn)
    with pytest.raises(configio.ImportConflict):
        _apply(conn, _doc(routes={"r": {"target": {"group": "ghost"}}}), tmp_path=tmp_path)
    assert list((tmp_path / "backups").iterdir()) == []


def test_merge_writes_no_backup(conn, tmp_path):
    r = _apply(conn, "version: 1\n", "merge", tmp_path=tmp_path)
    assert r.backup is None and not (tmp_path / "backups").exists()

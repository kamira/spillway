"""`GET /api/config/export`(CHG-20261102-02 T3;決策 5、19;spec 3、9.1.4)與
`POST /api/config/import`(T4;決策 6、7、8、20;檔尾段落)。

L1:舊列 `params`/`capabilities` 含憑證鍵名或疑似金鑰值 → 422 `validation_error`,只回位置、不回顯值。
L2:Content-Type、Content-Disposition(UTC 檔名,時間取自注入的 clock)、Cache-Control、nosniff;
內容等於 `configio.dump`(黃金檔);不含假金鑰值與 token;Host/Origin 照常適用;匯出不寫資料庫。
"""
from __future__ import annotations

import contextlib
import json
import logging
import stat
import time
from pathlib import Path

import pytest
import yaml

from spillway import configio
from spillway.app import create_app
from spillway.clock import SeqRng
from spillway.domain import Deployment
from spillway.store import repo

from tests.helpers.config_golden import GOLDEN, NOW, seed_golden

from .helpers import KEY, api_member, assert_error

pytestmark = pytest.mark.anyio

EXPORT = "/api/config/export"
LEGACY_VALUE = "legacy-value-q9z7"   # 舊列中的假憑證值:不可出現在回應中


def _snapshot(db) -> list[str]:
    return list(db.iterdump())


async def test_export_headers_and_body(client, db, clock):
    seed_golden(db)
    r = await client.get(EXPORT)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/yaml; charset=utf-8"
    assert r.headers["content-disposition"] == (
        'attachment; filename="spillway-config-20261030T120000Z.yaml"')
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.content == GOLDEN.read_bytes()
    assert r.text == configio.dump(db)


async def test_filename_follows_clock(client, clock):
    clock.advance(days=3, hours=1, minutes=2, seconds=3)
    r = await client.get(EXPORT)
    assert r.status_code == 200
    assert 'filename="spillway-config-20261102T130203Z.yaml"' in r.headers["content-disposition"]


async def test_empty_export(client):
    r = await client.get(EXPORT)
    assert r.status_code == 200
    assert r.text == "version: 1\nsettings: {}\ndeployments: []\ngroups: {}\nroutes: {}\n"


async def test_export_has_no_secret_or_token(client, db):
    r = await client.post("/api/deployments", json=api_member("api-x"))
    assert r.status_code == 201, r.text
    token = (await client.post("/api/settings/token/rotate")).json()["token"]
    token_hash = repo.get_setting(db, "access_token_sha256")
    r = await client.get(EXPORT)
    assert r.status_code == 200
    assert "credential_ref: env:SPILLWAY_TEST_KEY" in r.text
    for needle in (KEY, token, token_hash):
        assert needle not in r.text


async def test_export_does_not_write(client, db):
    seed_golden(db)
    before = _snapshot(db)
    assert (await client.get(EXPORT)).status_code == 200
    assert _snapshot(db) == before


@pytest.mark.parametrize("column,value,path", [
    ("params", {"access_token": LEGACY_VALUE}, "deployments[legacy].params.access_token"),
    ("params", {"nested": {"api-key": LEGACY_VALUE}}, "deployments[legacy].params.nested.api-key"),
    ("params", {"note": KEY}, "deployments[legacy].params.note"),
    ("capabilities", {"ctx": 1, "secret": LEGACY_VALUE},
     "deployments[legacy].capabilities.secret"),
])
async def test_legacy_secret_rejected(client, db, column, value, path):
    db.execute(
        "INSERT INTO deployments (id, model_name, kind, provider, model, params, capabilities) "
        "VALUES ('legacy', 'm', 'api', 'openai', 'gpt', ?, ?)",
        (json.dumps(value if column == "params" else {}),
         json.dumps(value if column == "capabilities" else {})),
    )
    r = await client.get(EXPORT)
    msg = assert_error(r, 422, "validation_error")
    assert path in msg
    assert LEGACY_VALUE not in r.text   # 不回顯值
    assert KEY not in r.text
    assert "content-disposition" not in r.headers


async def test_legacy_hits_all_listed(client, db):
    for dep_id in ("old-a", "old-b"):
        db.execute(
            "INSERT INTO deployments (id, model_name, kind, provider, model, params) "
            "VALUES (?, 'm', 'api', 'openai', 'gpt', ?)", (dep_id, json.dumps({"password": LEGACY_VALUE})))
    msg = assert_error(await client.get(EXPORT), 422, "validation_error")
    assert "deployments[old-a].params.password" in msg
    assert "deployments[old-b].params.password" in msg
    assert LEGACY_VALUE not in msg


@pytest.mark.parametrize("headers", [
    {"Origin": "http://evil.example"},
    {"Host": "evil.example:8787"},
])
async def test_host_origin_apply(client, db, headers):
    seed_golden(db)
    r = await client.get(EXPORT, headers=headers)
    assert r.status_code == 403, r.text
    assert "deployments" not in r.text


async def test_same_origin_allowed(client):
    r = await client.get(EXPORT, headers={"Origin": "http://127.0.0.1:8787"})
    assert r.status_code == 200


# ================================================================ POST /api/config/import(T4)
# L1:請求邊界(mode、Content-Type、大小、UTF-8)、YAML 與欄位驗證、single、privileged、dry_run;
# L2:replace/merge 的保留與刪除語意、冷卻、健康快取、備份、往返(決策 8)、事件。

IMPORT = "/api/config/import"
YAML_CT = {"Content-Type": "application/yaml"}
FAKE_AIZA = "AIzaSyD-fake-0000-test-key-q9z7"
CONFIG_TABLES = ("deployments", "deployment_tags", "budgets", "groups", "routes", "route_targets")
BACKUP_NAME = "config-20261030T120000Z.yaml"     # FakeClock 預設時間


def _yaml(**top) -> str:
    return yaml.safe_dump({"version": 1, **top}, sort_keys=False, allow_unicode=True)


def _golden_doc() -> dict:
    return yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))


def _dump_doc(doc: dict) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def _member(dep_id: str, **kw) -> dict:
    body = {"id": dep_id, "model_name": dep_id, "kind": "api", "provider": "openai",
            "model": "gpt-4o-mini"}
    body.update(kw)
    return body


def _cli(dep_id: str) -> dict:
    return {"id": dep_id, "model_name": dep_id, "kind": "cli", "provider": "claude-cli",
            "model": "claude-sonnet-4-5"}


async def _import(client, text: str | bytes, mode: str | None = "replace", *, dry_run: bool = False,
                  headers: dict | None = None):
    params = {} if mode is None else {"mode": mode}
    if dry_run:
        params["dry_run"] = "true"
    body = text.encode("utf-8") if isinstance(text, str) else text
    return await client.post(IMPORT, params=params, content=body,
                             headers=YAML_CT if headers is None else headers)


def _config_rows(conn) -> dict[str, list[tuple]]:
    out = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
           for t in CONFIG_TABLES}
    out["settings"] = [tuple(r) for r in conn.execute(
        "SELECT * FROM settings WHERE key != 'access_token_sha256' ORDER BY key")]
    return out


@contextlib.asynccontextmanager
async def _instrumented(make_app, make_client):
    """app 與 client;記錄 `router.invalidate_health` 與 `mcp_events.changed` 的呼叫。"""
    app = make_app()
    calls: dict = {"invalidate": [], "changed": 0}
    async with make_client(app) as c:
        router_obj = app.state.router
        orig = router_obj.invalidate_health

        def invalidate(dep_id):
            calls["invalidate"].append(dep_id)
            orig(dep_id)

        def changed():
            calls["changed"] += 1

        router_obj.invalidate_health = invalidate
        app.state.mcp_events.changed = changed
        yield app, c, calls


@contextlib.asynccontextmanager
async def _fresh(make_client, clock, tmp_path: Path, name: str):
    """另一個空資料目錄的 app 與 client。"""
    (tmp_path / name).mkdir()
    app = create_app(clock=clock, rng=SeqRng([0.0]), db_path=tmp_path / name / "spillway.db")
    async with make_client(app) as c:
        yield app, c


# ---------------------------------------------------------------- 請求邊界


async def test_import_mode_required(client, db):
    seed_golden(db)
    before = _snapshot(db)
    assert_error(await _import(client, "version: 1\n", mode=None), 422, "validation_error")
    msg = assert_error(await _import(client, "version: 1\n", mode="upsert"), 422, "validation_error")
    assert "mode" in msg
    assert _snapshot(db) == before


async def test_text_plain_cross_site_403_and_plain_422(client, db):
    seed_golden(db)
    before = _snapshot(db)
    r = await _import(client, "version: 1\n", headers={"Content-Type": "text/plain",
                                                     "Origin": "http://evil.example"})
    assert r.status_code == 403, r.text
    msg = assert_error(await _import(client, "version: 1\n", headers={"Content-Type": "text/plain"}),
                       422, "validation_error")
    assert "application/yaml" in msg
    assert _snapshot(db) == before


@pytest.mark.parametrize("ct", [None, "application/json", "application/x-www-form-urlencoded",
                                "multipart/form-data", "application/yaml; charset=latin-1"])
async def test_content_type_rejected(client, db, ct):
    headers = {} if ct is None else {"Content-Type": ct}
    assert_error(await _import(client, _yaml(deployments=[_member("a")]), headers=headers),
                 422, "validation_error")
    assert repo.list_deployments(db) == []


@pytest.mark.parametrize("ct", ["application/yaml", "application/x-yaml", "text/yaml",
                                "application/yaml; charset=utf-8", "Text/YAML; Charset=UTF-8"])
async def test_content_type_accepted(client, ct):
    r = await _import(client, _yaml(deployments=[_member("a")]), "merge", headers={"Content-Type": ct})
    assert r.status_code == 200, r.text


async def test_host_origin_apply_to_import(client, db):
    for headers in ({**YAML_CT, "Origin": "http://evil.example"}, {**YAML_CT, "Host": "evil.example"}):
        r = await _import(client, _yaml(deployments=[_member("a")]), headers=headers)
        assert r.status_code == 403, r.text
    assert repo.list_deployments(db) == []


async def test_content_length_over_limit(client):
    big = b"version: 1\n" + b"#" * (2 * 1024 * 1024)
    msg = assert_error(await _import(client, big), 422, "validation_error")
    assert "1048576" in msg


async def test_chunked_body_over_limit(client, db):
    seed_golden(db)
    before = _snapshot(db)

    async def gen():
        chunk = b"#" + b"x" * 65534 + b"\n"
        for _ in range(32):          # 2 MiB,不帶 Content-Length(chunked)
            yield chunk

    r = await client.post(IMPORT, params={"mode": "replace"}, content=gen(), headers=YAML_CT)
    assert "content-length" not in {k.lower() for k in r.request.headers}
    assert "1048576" in assert_error(r, 422, "validation_error")
    assert _snapshot(db) == before


async def test_exactly_limit_is_read(client):
    body = b"version: 1\n"
    body += b"#" * (1_048_576 - len(body))
    r = await _import(client, body, "merge")
    assert r.status_code == 200, r.text


async def test_utf8_bom_and_invalid_utf8(client):
    r = await _import(client, "﻿" + _yaml(deployments=[_member("a")]), "merge")
    assert r.status_code == 200, r.text
    assert_error(await _import(client, b"version: 1\nx: \xff\xfe\n"), 422, "validation_error")


# ---------------------------------------------------------------- YAML 與欄位驗證


@pytest.mark.parametrize("text,needle", [
    ("version: 1\na: &x 1\nb: *x\n", "anchor/alias"),
    ("version: 1\nversion: 1\n", "version: 重複的鍵"),
    ("version: 1\n1: a\n", "鍵必須是字串"),
    ("version: 1\nx: !!binary aGk=\n", "tag"),
    ("version: 2\n", "version"),
    ("- a\n", "mapping"),
    ("version: 1\nx: \x01\n", "不允許的字元"),
    ('version: 1\ndeployments:\n  - id: "a\\ud800"\n', "deployments[0].id: 不可含孤立的代理字元"),
    ("version: 1\nsettings:\n  context_retention_days: 2026-01-01\n",
     "settings.context_retention_days"),
    ("version: 1\nsettings:\n  record_context_default: on\n", "settings.record_context_default"),
])
async def test_yaml_errors_422(client, db, text, needle):
    seed_golden(db)
    before = _snapshot(db)
    msg = assert_error(await _import(client, text), 422, "validation_error")
    assert needle in msg
    assert _snapshot(db) == before


async def test_yaml_bombs_fast(client):
    bomb = ["version: 1", 'a: &a ["x","x","x","x","x","x","x","x","x"]']
    for i in range(1, 9):
        prev, cur = chr(ord("a") + i - 1), chr(ord("a") + i)
        bomb.append(f"{cur}: &{cur} [{','.join(['*' + prev] * 9)}]")
    for text in ("\n".join(bomb) + "\n", "version: 1\na: &a [*a]\n", "[" * 50000):
        t0 = time.monotonic()
        assert_error(await _import(client, text), 422, "validation_error")
        assert time.monotonic() - t0 < 1


async def test_syntax_error_with_key_not_echoed(client, caplog):
    caplog.set_level(logging.DEBUG)
    text = f'version: 1\nnote: "{FAKE_AIZA}\n  oops: [\n'
    r = await _import(client, text)
    assert "yaml: line " in assert_error(r, 422, "validation_error")
    assert "AIza" not in r.text
    assert "AIza" not in caplog.text


@pytest.mark.parametrize("text", [
    f"version: 1\nx: !{FAKE_AIZA}!v 1\n",
    f"%TAG !{FAKE_AIZA}! tag:a,2000:\n%TAG !{FAKE_AIZA}! tag:b,2000:\n---\nversion: 1\n",
    "version: 1\nx: " + "9" * 5000 + "\n",
])
async def test_parser_problem_not_echoed(client, caplog, text):
    caplog.set_level(logging.DEBUG)
    r = await _import(client, text)
    assert_error(r, 422, "validation_error")
    assert "AIza" not in r.text and "9999" not in r.text
    assert "AIza" not in caplog.text


async def test_field_errors_all_paths_and_no_change(client, db):
    seed_golden(db)
    before = _snapshot(db)
    doc = _yaml(deployments=[_member("a"), _member("b", kind="bogus"), _member("c", color="x")],
                groups={"..": {}}, routes={"r": {"target": {"group": "g"}, "targets": [
                    [{"deployment": "a"}], [{"deployment": "a"}]]}})
    msg = assert_error(await _import(client, doc), 422, "validation_error")
    for path in ("deployments[1].kind", "deployments[2].color", 'groups[".."]', "routes.r.targets"):
        assert path in msg, msg
    assert _snapshot(db) == before


async def test_params_secret_not_echoed(client, db):
    for params in ({"note": KEY}, {"api_key": LEGACY_VALUE}, {"signals": {"bogus": []}}):
        r = await _import(client, _yaml(deployments=[_member("a", params=params)]))
        msg = assert_error(r, 422, "validation_error")
        assert "deployments[0].params" in msg
        assert KEY not in r.text and LEGACY_VALUE not in r.text
    assert repo.list_deployments(db) == []


async def test_targets_unknown_member_422(client, db):
    seed_golden(db)
    before = _snapshot(db)
    doc = _yaml(routes={"r": {"target": {"tags": {}}, "targets": [[{"deployment": "ghost"}]]}})
    for mode in ("merge", "replace"):
        msg = assert_error(await _import(client, doc, mode), 422, "validation_error")
        assert "routes.r.targets[0][0].deployment" in msg
    assert _snapshot(db) == before


async def test_target_group_missing_422(client, db):
    msg = assert_error(await _import(client, _yaml(routes={"r": {"target": {"group": "ghost"}}})),
                       422, "validation_error")
    assert "routes.r.target.group" in msg
    assert repo.list_routes(db) == []


async def test_single_conflicts_409(client, db):
    msg = assert_error(await _import(client, _yaml(deployments=[_cli("c1"), _cli("c2")]), "merge"),
                       409, "provider_single")
    assert "c1" in msg and "c2" in msg
    assert repo.list_deployments(db) == []
    r = await _import(client, _yaml(deployments=[_cli("c1"), _member("x")]), "merge")
    assert r.status_code == 200, r.text
    before = _snapshot(db)
    changed = {**_member("x"), "kind": "cli", "provider": "claude-cli"}
    assert_error(await _import(client, _yaml(deployments=[changed]), "merge"), 409, "provider_single")
    assert _snapshot(db) == before


# ---------------------------------------------------------------- privileged 與 dry_run(決策 20)


async def test_privileged_in_response(client):
    base = [_member("api-x", params={"api_base": "http://127.0.0.1:1/v1", "timeout": 5}),
            _member("plain")]
    r = await _import(client, _yaml(deployments=base))
    assert r.status_code == 200, r.text
    assert r.json()["privileged"] == ["api-x"]
    changed = [_member("api-x", params={"api_base": "http://127.0.0.1:1/v1", "timeout": 9}),
               _member("plain", params={"base_url": "http://127.0.0.1:2"},
                       credential_ref="env:SPILLWAY_TEST_KEY")]
    r = await _import(client, _yaml(deployments=changed), "merge")
    assert r.json()["privileged"] == ["plain"]
    assert "127.0.0.1" not in r.text and "SPILLWAY_TEST_KEY" not in r.text


async def test_dry_run_no_side_effects(make_app, make_client, db, db_path):
    seed_golden(db)
    async with _instrumented(make_app, make_client) as (app, c, calls):
        app.state.router.health_cache.put("api-a", False)
        before = _snapshot(db)
        doc = _golden_doc()
        doc["deployments"] = [d for d in doc["deployments"] if d["id"] != "bare"]
        doc["deployments"][1]["params"] = {"api_base": "http://127.0.0.1:9/v1"}   # api-b
        text = _dump_doc(doc)
        r = await _import(c, text, dry_run=True)
        assert r.status_code == 200, r.text
        dry = r.json()
        assert dry["dry_run"] is True and dry["backup"] is None
        assert dry["privileged"] == ["api-b"]
        assert dry["deleted"] == {"deployments": 1, "groups": 0, "routes": 0}
        assert _snapshot(db) == before
        assert not (db_path.parent / "backups").exists()
        assert calls == {"invalidate": [], "changed": 0}
        assert app.state.router.health_cache.get("api-a") is False
        r = await _import(c, text)
        assert r.status_code == 200, r.text
        real = r.json()
        assert real["dry_run"] is False and real["backup"] == BACKUP_NAME
        assert {k: v for k, v in dry.items() if k not in ("dry_run", "backup")} == {
            k: v for k, v in real.items() if k not in ("dry_run", "backup")}


async def test_dry_run_failure_same_code(client):
    for text, status, code in (
            (_yaml(deployments=[_member("a", kind="bogus")]), 422, "validation_error"),
            (_yaml(deployments=[_cli("c1"), _cli("c2")]), 409, "provider_single"),
            (_yaml(routes={"r": {"target": {"group": "ghost"}}}), 422, "validation_error")):
        a = await _import(client, text, dry_run=True)
        b = await _import(client, text)
        assert_error(a, status, code)
        assert a.json() == b.json()


# ---------------------------------------------------------------- replace / merge 語意


async def test_replace_preserves_runtime_state(make_app, make_client, db, db_path):
    seed_golden(db)
    repo.set_setting(db, "custom_key", "keep-me")
    repo.mark_cooldown(db, "bare", NOW, "quota_exhausted")
    ledger_before = db.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0]
    expected_backup = configio.dump(db)
    async with _instrumented(make_app, make_client) as (app, c, calls):
        app.state.router.health_cache.put("api-a", True)
        app.state.router.health_cache.put("bare", True)
        doc = _golden_doc()
        doc["deployments"] = [d for d in doc["deployments"] if d["id"] != "api-a"]
        doc["routes"]["review"]["targets"] = [
            [e for e in s if e["deployment"] != "api-a"] for s in doc["routes"]["review"]["targets"]]
        doc["settings"] = {"context_retention_days": 30}
        r = await _import(c, _dump_doc(doc))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mode"] == "replace"
        assert body["created"] == {"deployments": 0, "groups": 0, "routes": 0}
        assert body["updated"] == {"deployments": 4, "groups": 3, "routes": 3}
        assert body["deleted"] == {"deployments": 1, "groups": 0, "routes": 0}
        # 健康快取:交易前 ∪ 交易後的 id 逐一失效;tools/list_changed 一次
        assert sorted(calls["invalidate"]) == ["api-a", "api-b", "bare", "claude-sub", "qwen-local"]
        assert calls["changed"] == 1
        assert app.state.router.health_cache.get("api-a") is None
        assert app.state.router.health_cache.get("bare") is None
    assert db.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0] == ledger_before
    assert db.execute("SELECT COUNT(*) FROM usage_payloads").fetchone()[0] == 1
    assert repo.get_setting(db, "access_token_sha256") == "f" * 64
    assert repo.get_setting(db, "custom_key") == "keep-me"
    ctx = repo.get_context_settings(db)
    assert (ctx.record_default, ctx.retention_days, ctx.max_bytes) == (True, 30, 262144)
    assert db.execute(
        "SELECT COUNT(*) FROM settings WHERE key IN ('record_context_default', 'context_max_bytes')"
    ).fetchone()[0] == 0
    cooldowns = repo.list_cooldowns(db)
    assert "api-a" not in cooldowns and "bare" in cooldowns     # 被刪成員的冷卻跟著消失
    backup = db_path.parent / "backups" / body["backup"]
    assert body["backup"] == BACKUP_NAME
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    content = backup.read_text(encoding="utf-8")
    assert content == expected_backup                             # 內容等於匯入前匯出
    for needle in (KEY, "f" * 64, "ledger-only-text", "payload-only-text"):
        assert needle not in content


@pytest.mark.parametrize("mutate,status,code,path", [
    # route review 仍指向 coding,但檔案沒有 coding → replace 已刪除群組後的最終檢查失敗
    (lambda doc: doc["groups"].pop("coding"), 422, "validation_error", "routes.review.target.group"),
    # 兩個 claude-cli 成員:刪除 bare 之後才在最終檢查發現
    (lambda doc: doc["deployments"].__setitem__(2, {**_cli("claude-2")}), 409, "provider_single",
     "deployments"),
])
async def test_replace_fails_after_deletes_db_unchanged(client, db, db_path, mutate, status, code,
                                                        path):
    seed_golden(db)
    before = _snapshot(db)              # 全 DB iterdump(含帳本、payload、token、冷卻)
    doc = _golden_doc()
    mutate(doc)
    msg = assert_error(await _import(client, _dump_doc(doc)), status, code)
    assert msg.startswith(f"{path}: "), msg
    assert _snapshot(db) == before
    backups = db_path.parent / "backups"
    assert not backups.exists() or list(backups.iterdir()) == []   # 失敗時不留備份


async def test_replace_backup_not_overwritten(client, db_path):
    backups = db_path.parent / "backups"
    backups.mkdir()
    (backups / BACKUP_NAME).write_text("keep", encoding="utf-8")
    r = await _import(client, "version: 1\n")
    assert r.json()["backup"] == "config-20261030T120000Z-2.yaml"
    assert (backups / BACKUP_NAME).read_text(encoding="utf-8") == "keep"


async def test_replace_backup_symlink_500(client, db, db_path, tmp_path):
    seed_golden(db)
    before = _snapshot(db)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (db_path.parent / "backups").symlink_to(elsewhere)
    assert_error(await _import(client, "version: 1\n"), 500, "internal_error")
    assert _snapshot(db) == before
    assert list(elsewhere.iterdir()) == []


async def test_merge_member_only_keeps_other_routes(make_app, make_client, db, db_path):
    seed_golden(db)
    repo.set_setting(db, "context_retention_days", 99)
    before_targets = repo.list_all_route_targets(db)
    async with _instrumented(make_app, make_client) as (app, c, calls):
        r = await _import(c, _yaml(deployments=[_member("api-a", tags=["new"])]), "merge")
        assert r.status_code == 200, r.text
        assert r.json()["updated"] == {"deployments": 1, "groups": 0, "routes": 0}
        assert r.json()["deleted"] == {"deployments": 0, "groups": 0, "routes": 0}
        assert r.json()["backup"] is None
        assert sorted(calls["invalidate"]) == ["api-a", "api-b", "bare", "claude-sub", "qwen-local"]
        assert calls["changed"] == 1
    assert repo.list_all_route_targets(db) == before_targets        # review 的 targets 仍含 api-a
    assert "api-a" in repo.list_cooldowns(db)                       # 冷卻仍在
    dep = repo.load_deployment(db, "api-a")
    assert dep.tags == ("new",) and dep.credential_ref is None      # 成員整筆取代
    assert repo.get_setting(db, "context_retention_days") == "99"   # 沒有 settings 時不動
    assert len(repo.list_deployments(db)) == 5
    assert not (db_path.parent / "backups").exists()


async def test_merge_route_replaced_whole(client, db):
    seed_golden(db)
    r = await _import(client, _yaml(routes={"review": {"target": {"group": "mixed"}}}), "merge")
    assert r.status_code == 200, r.text
    assert repo.get_route(db, "review") == repo.Route(name="review", target={"group": "mixed"})
    assert repo.list_route_targets(db, "review") == []              # 未列 targets → 預設順序
    assert {x.name for x in repo.list_routes(db)} == {"quick", "review", "silent"}
    assert len(repo.list_groups(db)) == 3


async def test_merge_settings_only_listed_keys(client, db):
    seed_golden(db)
    r = await _import(client, _yaml(settings={"context_max_bytes": 4096}), "merge")
    assert r.status_code == 200, r.text
    ctx = repo.get_context_settings(db)
    assert (ctx.record_default, ctx.retention_days, ctx.max_bytes) == (False, 30, 4096)


async def test_replace_empty_file_clears_config(client, db):
    seed_golden(db)
    r = await _import(client, "version: 1\n")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == {"deployments": 5, "groups": 3, "routes": 3}
    assert (await client.get(EXPORT)).text == (
        "version: 1\nsettings: {}\ndeployments: []\ngroups: {}\nroutes: {}\n")
    assert (await client.get("/api/settings")).json()["first_run"] is True


# ---------------------------------------------------------------- 往返(決策 8;AC-10)


async def test_round_trip_golden(client, db, make_client, clock, tmp_path):
    seed_golden(db)
    exported = (await client.get(EXPORT)).content
    assert exported == GOLDEN.read_bytes()
    async with _fresh(make_client, clock, tmp_path, "empty") as (app2, c2):
        r = await c2.post(IMPORT, params={"mode": "replace"}, content=exported, headers=YAML_CT)
        assert r.status_code == 200, r.text
        assert r.json()["created"] == {"deployments": 5, "groups": 3, "routes": 3}
        assert (await c2.get(EXPORT)).content == exported          # 逐位元組相同
        conn2 = app2.state.conn_factory()
        try:
            assert _config_rows(conn2) == _config_rows(db)         # DB 全表快照等於原 DB
        finally:
            conn2.close()


async def test_round_trip_stale_target_and_slot_gap(client, db, make_client, clock, tmp_path):
    repo.save_deployment(db, Deployment(id="x", model_name="x", kind="local",
                                        provider="hosted_vllm", model="m", tags=("other",)))
    repo.save_deployment(db, Deployment(id="y", model_name="y", kind="local",
                                        provider="hosted_vllm", model="m", tags=("t",)))
    repo.save_route(db, repo.Route(name="r", target={"tags": {"all": ["t"], "any": [], "none": []}}))
    repo.replace_route_targets(db, "r", [         # x 不屬於目標(stale);slot 1、3(空洞)
        repo.RouteTarget("x", slot=1, position=0), repo.RouteTarget("y", slot=3, position=0)])
    exported = (await client.get(EXPORT)).content
    assert b"deployment: x" in exported
    async with _fresh(make_client, clock, tmp_path, "empty2") as (_app2, c2):
        r = await c2.post(IMPORT, params={"mode": "replace"}, content=exported, headers=YAML_CT)
        assert r.status_code == 200, r.text
        assert (await c2.get(EXPORT)).content == exported
    r = await client.post(IMPORT, params={"mode": "replace"}, content=exported, headers=YAML_CT)
    assert r.status_code == 200, r.text
    assert (await client.get(EXPORT)).content == exported
    assert [t.slot for t in repo.list_route_targets(db, "r")] == [1, 2]   # 空洞重新編號

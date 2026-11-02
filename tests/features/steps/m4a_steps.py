"""m4a_config.feature 專用步驟(CHG-20261102-02 T6;使用者故事 1–6;AC-10;NFR-4;spec 3、9.2;決策 20)。

驅動方式:
- 一律經 `context.gw`(create_app + lifespan + httpx ASGITransport)打管理 API;匯出、匯入、CSV、總覽
  都是真實 HTTP 介面。
- 「另一個空的閘道資料目錄」:在同一個情境暫存目錄下另開 `spillway-home-2`,以同一個 `Gateway` 類別
  (同一個假上游)建第二個閘道,存於 `context.m4a_other`;收尾登記到 `context.scenario_teardown`
  (environment.py 在刪除暫存目錄之前執行)。之後的「匯入回應…」「再次匯出…」「dry-run…」步驟作用在
  最近一次匯入的閘道(`_target`)。
- 存取 token:匯出、總覽前若尚未產生,直接在資料庫 rotate;明文只存在 `context.m4a_token`(行程記憶體),
  用於「不含存取 token 明文」的搜尋。
- 「已匯出目前設定為檔案 A」與「備份內容等於匯入前的匯出」共用同一個匯出函式 `_export_text`
  (每個匯入步驟送出前先以它記下目標閘道的匯出 `context.m4a_pre_export`,並記下全表快照
  `context.m4a_pre_dump` 與帳本列數)。
- 最近一次管理 API 回應同時存於 `context.admin_resp`,所以「管理 API 回應狀態為 … 且錯誤碼為 …」沿用
  `cleanup_admin_steps.py` 的既有步驟。

Background 中與其他 feature 共用的文字(`一個空的閘道資料目錄與假 API 上游`、`成員 … 類型 "api" …`、
`群組 … 的選擇器含 …`、`task route … 的 target 為群組 …,順序為 …`)沿用既有步驟,不在此重複註冊。
"""
from __future__ import annotations

import csv
import io
import json
import os
from typing import Any

import httpx
import yaml
from behave import step, use_step_matcher

from spillway import settings
from tests.features.steps.common_steps import _parse_slots
from tests.features.steps.m3a_steps import _cli_body, _ids
from tests.features.support.gateway import DEFAULT_MESSAGES, TEST_KEY, TEST_KEY_ENV, Gateway
from tests.helpers import redact_tokens

use_step_matcher("re")

EXPORT = "/api/config/export"
IMPORT = "/api/config/import"
CSV_PATH = "/api/ledger/export.csv"
YAML_CT = {"Content-Type": "application/yaml"}
CSV_PROMPT = "m4a-csv-prompt-text-4242"      # 請求內容:不可出現在 CSV
TASK_META = {"mode": "task"}


# ---------------------------------------------------------------- 驅動


def _teardown(context, fn) -> None:
    context.scenario_teardown.append(fn)


def _target(context) -> Gateway:
    """最近一次匯入的閘道(沒有另一個資料目錄時為 `context.gw`)。"""
    return getattr(context, "m4a_target", None) or context.gw


def _other(context) -> Gateway:
    gw = getattr(context, "m4a_other", None)
    if gw is None:
        home = context.tmp / "spillway-home-2"
        home.mkdir()
        assert not any(home.iterdir())
        gw = context.m4a_other = Gateway(home, context.gw.fake)
        _teardown(context, gw.close)
    return gw


def _secrets(context) -> list[str]:
    return [s for s in (TEST_KEY, getattr(context, "m4a_token", None)) if s]


def _safe(context, text: str) -> str:
    return redact_tokens(text, _secrets(context))


def _ensure_token(context, gw: Gateway | None = None) -> str:
    gw = gw or context.gw
    gw.ensure_started()
    with gw.db() as conn:
        if settings.get_access_token_hash(conn) is None:
            context.m4a_token = settings.rotate_access_token(conn)
    tok = getattr(context, "m4a_token", None)
    assert tok, "前置條件:存取 token 已由本步驟檔產生(明文只在行程記憶體)"
    return tok


def _export(gw: Gateway) -> httpx.Response:
    resp = gw.get(EXPORT)
    assert resp.status_code == 200, f"匯出失敗 {resp.status_code}"
    return resp


def _export_text(gw: Gateway) -> str:
    """「已匯出…檔案 A」與「備份內容等於匯入前的匯出」共用的匯出函式。"""
    return _export(gw).text


def _dump(gw: Gateway) -> list[str]:
    gw.ensure_started()
    with gw.db() as conn:
        return list(conn.iterdump())


def _exported(gw: Gateway) -> dict[str, Any]:
    return yaml.safe_load(_export_text(gw))


def _dep(doc: dict[str, Any], dep_id: str) -> dict[str, Any]:
    found = [d for d in doc["deployments"] if d["id"] == dep_id]
    assert len(found) == 1, f"匯出檔沒有成員 {dep_id}"
    return found[0]


def _backups(gw: Gateway) -> list[Any]:
    d = gw.home / "backups"
    return sorted(p for p in d.iterdir()) if d.exists() else []


def _import(context, doc_or_text: Any, mode: str, *, dry_run: bool = False,
            gw: Gateway | None = None) -> httpx.Response:
    gw = gw or context.gw
    gw.ensure_started()
    text = doc_or_text if isinstance(doc_or_text, str) else yaml.safe_dump(
        doc_or_text, sort_keys=False, allow_unicode=True)
    # 匯入前狀態(備份比對、整批不寫入、帳本列數)
    context.m4a_pre_export = _export_text(gw)
    context.m4a_pre_dump = _dump(gw)
    context.m4a_pre_ledger = gw.ledger_total()
    context.m4a_pre_backups = _backups(gw)
    context.m4a_pre_get = {}
    for path in ("/api/deployments/api-a", "/api/routes/review"):
        r = gw.get(path)
        context.m4a_pre_get[path] = r.json() if r.status_code == 200 else None
    params = {"mode": mode}
    if dry_run:
        params["dry_run"] = "true"
    resp = gw.post(IMPORT, params=params, content=text.encode("utf-8"), headers=YAML_CT)
    context.m4a_target = gw
    context.m4a_import = resp
    context.admin_resp = resp
    assert all(s not in resp.text for s in _secrets(context)), "匯入回應含憑證值或 token"
    return resp


def _api_doc(gw: Gateway, dep_id: str) -> dict[str, Any]:
    body = gw.api_body(dep_id)
    return {k: v for k, v in body.items() if k != "budget"}


# ---------------------------------------------------------------- Background


@step(r'成員 "([^"]+)" 的 credential_ref 為 "([^"]+)" 且該環境變數為測試假值')
def step_member_credential(context, dep_id, ref):
    gw = context.gw
    assert ref == f"env:{TEST_KEY_ENV}", ref
    if gw.deployment(dep_id).get("credential_ref") != ref:
        gw.update_member(dep_id, credential_ref=ref)
    assert gw.deployment(dep_id)["credential_ref"] == ref
    assert os.environ.get(TEST_KEY_ENV) == TEST_KEY, "測試假值未設定"


# ---------------------------------------------------------------- 匯出


@step(r"以管理 API 匯出設定")
def step_export(context):
    _ensure_token(context)
    resp = _export(context.gw)
    context.m4a_export_resp = resp
    context.m4a_export = yaml.safe_load(resp.text)


@step(r"回應為 YAML 且 version 為 (\d+)")
def step_export_yaml(context, version):
    resp = context.m4a_export_resp
    assert resp.headers["content-type"] == "application/yaml; charset=utf-8", resp.headers
    assert resp.headers["content-disposition"].startswith('attachment; filename="spillway-config-'), \
        resp.headers
    assert context.m4a_export["version"] == int(version), context.m4a_export.get("version")


@step(r'匯出檔含成員 (.+),群組 "([^"]+)",route "([^"]+)"')
def step_export_contains(context, members, group, route):
    doc = context.m4a_export
    assert [d["id"] for d in doc["deployments"]] == sorted(_ids(members)), doc["deployments"]
    assert group in doc["groups"], doc["groups"]
    assert route in doc["routes"], doc["routes"]
    assert doc["groups"][group] == {"any": sorted(_ids(members))}, doc["groups"][group]
    assert doc["routes"][route]["target"] == {"group": group}, doc["routes"][route]


@step(r'route "([^"]+)" 的 targets 依序為 (slot .+)')
def step_export_targets(context, route, order):
    targets = context.m4a_export["routes"][route]["targets"]
    got = [[e["deployment"] for e in slot] for slot in targets]
    assert got == _parse_slots(order), targets


# 與 Background 的「… 且該環境變數為測試假值」不重疊:`use_step_matcher("re")` 為 behave 的
# SimplifiedRegexMatcher,登記時自動編譯成 `^…$`(全句比對);pattern 內自行寫 `$` 會被 behave 拒絕
# (ValueError: should not use begin/end-markers)。
@step(r'成員 "([^"]+)" 的 credential_ref 為 "([^"]+)"')
def step_export_credential(context, dep_id, ref):
    assert _dep(context.m4a_export, dep_id)["credential_ref"] == ref


@step(r"匯出檔不含該環境變數的值,也不含存取 token 明文")
def step_export_no_secret(context):
    raw = context.m4a_export_resp.content
    tok = getattr(context, "m4a_token", None)
    assert tok, "前置條件:已產生存取 token"
    hits = [name for name, s in (("env value", TEST_KEY), ("token", tok)) if s.encode() in raw]
    assert hits == [], hits
    with context.gw.db() as conn:
        token_hash = settings.get_access_token_hash(conn)
    assert token_hash and token_hash.encode() not in raw, "匯出檔含 token 雜湊"


# ---------------------------------------------------------------- 往返


@step(r"已匯出目前設定為檔案 A")
def step_exported_file_a(context):
    _ensure_token(context)
    context.m4a_file_a = _export_text(context.gw)
    assert TEST_KEY not in context.m4a_file_a and context.m4a_token not in context.m4a_file_a


@step(r'在另一個空的閘道資料目錄以 mode "(merge|replace)" 匯入檔案 A')
def step_import_a_other(context, mode):
    other = _other(context)
    _import(context, context.m4a_file_a, mode, gw=other)


@step(r"匯入回應狀態為 (\d+)")
def step_import_status(context, status):
    resp = context.m4a_import
    assert resp.status_code == int(status), f"{resp.status_code} {_safe(context, resp.text)}"


@step(r"再次匯出的內容與檔案 A 相同")
def step_reexport_same(context):
    again = _export(_target(context)).content
    assert again == context.m4a_file_a.encode("utf-8"), "再次匯出的內容與檔案 A 不同"


@step(r'dry-run route "([^"]+)" 的第一順位為 "([^"]+)"')
def step_dry_run_first(context, route, dep_id):
    gw = _target(context)
    plan = gw.dry_run(route)
    first = plan["slots"][0]["entries"][0]["deployment"]
    assert first == dep_id, json.dumps(plan, ensure_ascii=False)
    # 與原閘道的第一順位相同
    if gw is not context.gw:
        orig = context.gw.dry_run(route)["slots"][0]["entries"][0]["deployment"]
        assert orig == first, (orig, first)


# ---------------------------------------------------------------- merge


@step(r'以 mode "merge" 匯入只含成員 "([^"]+)" 且其 tags 為 (\[.*\]) 的設定')
def step_import_merge_tags(context, dep_id, tags_json):
    tags = json.loads(tags_json)
    dep = dict(_dep(_exported(context.gw), dep_id))     # 由現有成員的匯出組成,只改 tags
    dep["tags"] = tags
    context.m4a_sent_tags = tags
    _import(context, {"version": 1, "deployments": [dep]}, "merge")


@step(r'成員 "([^"]+)" 的 tags 為 (\[.*\])')
def step_member_tags(context, dep_id, tags_json):
    got = _target(context).deployment(dep_id)["tags"]
    # tags 是集合(存放時依字母排序,repo.set_tags)
    assert sorted(got) == sorted(json.loads(tags_json)), got


@step(r'成員 "([^"]+)" 與 route "([^"]+)" 不變')
def step_member_route_unchanged(context, dep_id, route):
    gw = _target(context)
    assert gw.deployment(dep_id) == context.m4a_pre_get[f"/api/deployments/{dep_id}"]
    assert gw.ok(gw.get(f"/api/routes/{route}"), 200) == context.m4a_pre_get[f"/api/routes/{route}"]
    # 除了該成員的 tags 之外,其他列(含 groups、route_targets、settings)不變
    changed = sorted(set(_dump(gw)) ^ set(context.m4a_pre_dump))
    assert changed and all("deployment_tags" in ln for ln in changed), changed


# ---------------------------------------------------------------- 驗證失敗


@step(r'以 mode "(merge|replace)" 匯入第二個成員的 kind 為 "([^"]+)" 的設定')
def step_import_bad_kind(context, mode, kind):
    doc = _exported(context.gw)
    assert len(doc["deployments"]) >= 2
    doc["deployments"][1]["kind"] = kind
    _import(context, doc, mode)


@step(r'錯誤訊息含欄位路徑 "([^"]+)"')
def step_error_path(context, path):
    err = context.admin_resp.json()["error"]
    assert path in err["message"], err


@step(r"成員、群組與 route 皆與匯入前相同")
def step_unchanged(context):
    gw = _target(context)
    assert _dump(gw) == context.m4a_pre_dump, "資料庫在匯入後改變了"
    assert _export_text(gw) == context.m4a_pre_export


# ---------------------------------------------------------------- replace 備份


@step(r'以 mode "replace" 匯入只含成員 "([^"]+)" 的設定')
def step_import_replace_only(context, dep_id):
    _import(context, {"version": 1, "deployments": [_api_doc(context.gw, dep_id)]}, "replace")


@step(r"資料目錄的 backups 下有一個備份檔,內容等於匯入前的匯出")
def step_backup_equals(context):
    gw = _target(context)
    new = [p for p in _backups(gw) if p not in context.m4a_pre_backups]
    assert len(new) == 1, new
    body = context.m4a_import.json()
    assert body["backup"] == new[0].name, (body["backup"], new[0].name)
    assert (new[0].stat().st_mode & 0o777) == 0o600, oct(new[0].stat().st_mode)
    assert new[0].read_bytes() == context.m4a_pre_export.encode("utf-8"), "備份內容不等於匯入前的匯出"
    assert all(s.encode() not in new[0].read_bytes() for s in _secrets(context))


@step(r"帳本列數與匯入前相同")
def step_ledger_same(context):
    assert _target(context).ledger_total() == context.m4a_pre_ledger


# ---------------------------------------------------------------- dry_run


@step(r'以 mode "(merge|replace)" 且 dry_run 匯入把成員 "([^"]+)" 的 params\.api_base 改為 "([^"]+)" 的設定')
def step_import_dry_run(context, mode, dep_id, api_base):
    doc = _exported(context.gw)
    dep = _dep(doc, dep_id)
    dep["params"] = {**dep.get("params", {}), "api_base": api_base}
    _import(context, doc, mode, dry_run=True)


@step(r"匯入回應的 dry_run 為 true 且 backup 為空")
def step_dry_run_flags(context):
    body = context.m4a_import.json()
    assert body["dry_run"] is True and body["backup"] is None, body


@step(r"匯入回應的 privileged 為 (\[.*\])")
def step_privileged(context, ids_json):
    assert context.m4a_import.json()["privileged"] == json.loads(ids_json), context.m4a_import.json()


@step(r"資料目錄的 backups 下沒有備份檔")
def step_no_backup(context):
    assert _backups(_target(context)) == [], _backups(_target(context))


# ---------------------------------------------------------------- single


@step(r'以 mode "(merge|replace)" 匯入兩個 provider 為 "(claude-cli)" 的成員')
def step_import_two_single(context, mode, provider):
    deps = [_cli_body(d, provider) for d in ("claude-one", "claude-two")]
    _import(context, {"version": 1, "deployments": deps}, mode)


# ---------------------------------------------------------------- 帳本 CSV


@step(r'帳本有 (\d+) 列,其中 (\d+) 列屬於 route "([^"]+)"')
def step_ledger_rows(context, total, in_route, route):
    gw = context.gw
    total, in_route = int(total), int(in_route)
    messages = [{"role": "user", "content": CSV_PROMPT}]
    for _ in range(in_route):
        r = gw.chat(route, messages=messages, metadata=dict(TASK_META))
        assert r.status_code == 200, _safe(context, r.text)
    for _ in range(total - in_route):
        r = gw.chat("api-a", messages=messages)          # 隱含 route(model_name)
        assert r.status_code == 200, _safe(context, r.text)
    assert gw.ledger_total() == total
    assert gw.ledger(route=route)["total"] == in_route
    with gw.db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM usage_payloads").fetchone()[0]
    assert n > 0, "前置條件:有請求內容被保存(CSV 不可含它)"


@step(r'以管理 API 匯出 route 為 "([^"]+)" 的帳本 CSV')
def step_export_csv(context, route):
    resp = context.gw.get(CSV_PATH, params={"route": route})
    assert resp.status_code == 200, _safe(context, resp.text)
    assert resp.headers["content-type"].startswith("text/csv"), resp.headers
    context.m4a_csv_resp = resp
    text = resp.content.decode("utf-8-sig")
    context.m4a_csv = list(csv.reader(io.StringIO(text)))
    context.m4a_csv_route = route


@step(r"CSV 有標題列與 (\d+) 列資料")
def step_csv_rows(context, n):
    rows = context.m4a_csv
    header, data = rows[0], rows[1:]
    assert header[:4] == ["id", "ts", "route_name", "deployment_id"], header
    assert len(data) == int(n), data
    col = header.index("route_name")
    assert all(r[col] == context.m4a_csv_route for r in data), data
    listed = {str(i["id"]) for i in context.gw.ledger(route=context.m4a_csv_route)["items"]}
    assert {r[0] for r in data} == listed


@step(r"CSV 不含任何請求或回應內容")
def step_csv_no_payload(context):
    raw = context.m4a_csv_resp.content.decode("utf-8-sig")
    assert CSV_PROMPT not in raw
    assert "fake upstream ok" not in raw          # 假上游回應文字
    assert all(m["content"] not in raw for m in DEFAULT_MESSAGES)
    assert TEST_KEY not in raw


# ---------------------------------------------------------------- 設定總覽


@step(r"以管理 API 取得設定總覽")
def step_settings_overview(context):
    _ensure_token(context)
    resp = context.gw.get("/api/settings")
    assert resp.status_code == 200, _safe(context, resp.text)
    context.m4a_overview_resp = resp


@step(r"總覽含 port、data_dir、token_issued、first_run 與 counts")
def step_overview_shape(context):
    gw = context.gw
    body = context.m4a_overview_resp.json()
    assert {"port", "data_dir", "token_issued", "first_run", "counts"} <= set(body), sorted(body)
    assert body["port"] == gw.app.state.port
    assert body["data_dir"] == str(gw.db_path.parent)
    assert body["token_issued"] is True and body["first_run"] is False, body
    assert body["counts"]["deployments"] == 2 and body["counts"]["groups"] == 1 \
        and body["counts"]["routes"] == 1, body["counts"]


@step(r"總覽不含存取 token 明文")
def step_overview_no_token(context):
    raw = context.m4a_overview_resp.text
    with context.gw.db() as conn:
        token_hash = settings.get_access_token_hash(conn)
    assert context.m4a_token not in raw and token_hash not in raw and TEST_KEY not in raw


# ---------------------------------------------------------------- 群組名稱


def _create_group(context, name: str) -> None:
    gw = context.gw
    context.admin_resp = gw.post("/api/groups", json={"name": name,
                                                      "selector": {"all": [], "any": ["api-a"], "none": []}})
    with gw.db() as conn:
        rows = conn.execute("SELECT name FROM groups WHERE name = ?", (name,)).fetchall()
    context.m4a_group_rows = rows


@step(r'以管理 API 的請求 body 建立名為 "([^"]+)" 的群組')
def step_group_named(context, name):
    _create_group(context, name)
    assert context.m4a_group_rows == []


@step(r"以管理 API 的請求 body 建立名稱含 U\+([0-9A-Fa-f]{4}) 的群組")
def step_group_codepoint(context, hexcode):
    _create_group(context, f"bad{chr(int(hexcode, 16))}name")
    assert context.m4a_group_rows == []

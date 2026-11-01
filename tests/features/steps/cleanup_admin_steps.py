"""m2c_cleanup.feature 的管理 API 步驟(CHG-20261101-02 T7;決策 1、4)。

本檔是本筆 feature 中所有「以管理 API …」「管理 API 回應…」「錯誤訊息…」「資料庫中…」步驟的唯一
擁有者(健康情境的 api_base 修改步驟也在此;健康專屬步驟見 `cleanup_health_steps.py`)。
最近一次管理 API 回應存於 `context.admin_resp`。
"""
from __future__ import annotations

import json
from typing import Any

from behave import step, use_step_matcher

use_step_matcher("re")


def _create_api_member(context, dep_id: str, params: dict[str, Any]) -> None:
    gw = context.gw
    body = gw.api_body(dep_id)
    body["params"] = {**body.get("params", {}), **params}
    context.admin_sent = body
    context.admin_resp = gw.create_member(body)


def _admin_error(context) -> dict[str, Any]:
    body = context.admin_resp.json()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message"}, body   # 9.1.2
    return body["error"]


# ---------------------------------------------------------------- 以管理 API …


@step(r'以管理 API 建立 api 成員 "([^"]+)",params 為 (\{.*\})')
def step_create_with_params(context, dep_id, params_json):
    params = json.loads(params_json)
    assert isinstance(params, dict), params_json
    _create_api_member(context, dep_id, params)


@step(r'以管理 API 建立 api 成員 "([^"]+)",params\.signals 覆寫 "(quota|rate_limit)" 規則為 HTTP 狀態 (\d+)')
def step_create_with_signals(context, dep_id, kind, status):
    _create_api_member(context, dep_id, {"signals": {kind: [{"http_status": int(status)}]}})


@step(r'以管理 API 建立 api 成員 "([^"]+)",params\.signals 含未知類別鍵 "([^"]+)"')
def step_create_with_unknown_signal_kind(context, dep_id, kind):
    _create_api_member(context, dep_id, {"signals": {kind: [{"http_status": 429}]}})


@step(r'以管理 API 把成員 "([^"]+)" 的 api_base 改為指向假 API 上游')
def step_update_api_base(context, dep_id):
    gw = context.gw
    assert dep_id in gw.members, f"成員 {dep_id} 不是經 gw.create_member 建立的"
    params = {**gw.members[dep_id]["body"].get("params", {}), "api_base": gw.fake.base_url + "/v1"}
    out = gw.update_member(dep_id, params=params)
    assert out["params"]["api_base"] == params["api_base"], out


# ---------------------------------------------------------------- 管理 API 回應…


@step(r"管理 API 回應狀態為 (\d+)")
def step_admin_status(context, status):
    resp = context.admin_resp
    assert resp.status_code == int(status), f"{resp.status_code} {resp.text}"


@step(r'管理 API 回應狀態為 (\d+) 且錯誤碼為 "([^"]+)"')
def step_admin_status_code(context, status, code):
    step_admin_status(context, status)
    err = _admin_error(context)
    assert err["code"] == code, err


# ---------------------------------------------------------------- 錯誤訊息…


@step(r'錯誤訊息含 "([^"]+)" 且不含 "([^"]+)"')
def step_error_message(context, present, absent):
    err = _admin_error(context)
    assert present in err["message"], err
    assert absent not in context.admin_resp.text, context.admin_resp.text


# ---------------------------------------------------------------- 資料庫中…


@step(r'資料庫中沒有成員 "([^"]+)"')
def step_db_no_member(context, dep_id):
    with context.gw.db() as conn:
        rows = conn.execute("SELECT id FROM deployments WHERE id = ?", (dep_id,)).fetchall()
    assert rows == [], rows


# ---------------------------------------------------------------- 讀回


@step(r'成員 "([^"]+)" 的 params\.signals 與送出的內容相同')
def step_signals_read_back(context, dep_id):
    sent = context.admin_sent
    assert sent["id"] == dep_id, sent
    got = context.gw.deployment(dep_id)["params"].get("signals")
    assert got == sent["params"]["signals"], got
    with context.gw.db() as conn:
        row = conn.execute("SELECT params FROM deployments WHERE id = ?", (dep_id,)).fetchone()
    assert row is not None and json.loads(row[0])["signals"] == sent["params"]["signals"], row

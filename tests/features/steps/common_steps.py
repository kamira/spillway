"""多個 feature 共用的步驟(CHG-20261030-02 T15)。

步驟一律以 `@step` 註冊(Given/When/Then/And 皆可用),regex 全句比對(behave 的 "re" matcher 自動加上首尾錨點),
文字與 feature 檔逐字一致。驅動物件 `context.gw` 見 `tests/features/support/gateway.py`。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from behave import step, use_step_matcher

use_step_matcher("re")

_SLOT_RE = re.compile(r'slot (\d+) "([^"]+)"')


def _parse_slots(text: str) -> list[list[str]]:
    found = [(int(n), [x.strip() for x in ids.split(",") if x.strip()]) for n, ids in _SLOT_RE.findall(text)]
    assert found, f"無法解析 slot 順序:{text}"
    numbers = [n for n, _ in found]
    assert numbers == list(range(1, len(found) + 1)), f"slot 編號必須自 1 連號:{text}"
    return [ids for _, ids in found]


def _parse_utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- 前置條件


@step(r"一個空的閘道資料目錄與假 API 上游")
def step_empty_home(context):
    gw = context.gw
    assert gw.home.is_dir() and not any(gw.home.iterdir()), "閘道資料目錄不是空的"
    r = gw.fake.requests
    assert r == [], "假上游已有請求紀錄"


@step(r'成員 "([^"]+)" 類型 "api" 優先權重 "(high|medium|low)" 額度比例 1')
def step_api_member(context, dep_id, weight):
    gw = context.gw
    gw.ok(gw.create_member(gw.api_body(dep_id), weight=weight), 201)
    # 額度比例 1:沒有 budgets 列 = 無上限,比例 1(決策 6)
    row = gw.dashboard_row(dep_id)
    assert row["ratio"] == 1, row


@step(r'local 成員 "([^"]+)",provider "([^"]+)",api_base 指向假 API 上游')
def step_local_member(context, dep_id, provider):
    gw = context.gw
    gw.ok(gw.create_member(gw.local_body(dep_id, provider)), 201)
    dep = gw.deployment(dep_id)
    assert dep["params"]["api_base"].startswith(gw.fake.base_url), dep


@step(r"加權抽選使用固定 seed 的亂數來源")
def step_fixed_seed(context):
    gw = context.gw
    gw.ensure_started()
    # app 以 gw.rng(SystemRng(seed=FIXED_SEED))建立;Router 使用同一個物件
    assert gw.app.state.rng is gw.rng
    assert gw.seed is not None


@step(r'target 只含所列成員的 route "([^"]+)" 順序為 (.+)')
def step_route(context, name, order):
    context.gw.create_route(name, _parse_slots(order))


@step(r'route "([^"]+)" 勾選預估額度檢查')
def step_route_precheck(context, name):
    gw = context.gw
    gw.update_route(name, budget_precheck=True)
    assert gw.ok(gw.get(f"/api/routes/{name}"), 200)["budget_precheck"] is True


@step(r'route "([^"]+)" 的 record_context 為 0')
def step_route_record_off(context, name):
    gw = context.gw
    gw.update_route(name, record_context=False)
    assert gw.ok(gw.get(f"/api/routes/{name}"), 200)["record_context"] is False


@step(r'系統時間為 "([^"]+)"')
def step_set_time(context, when):
    context.gw.clock.set(_parse_utc(when))


# ---------------------------------------------------------------- 假上游


@step(r'假上游對 "([^"]+)" 回傳 HTTP (\d+)')
def step_fake_member_status(context, dep_id, status):
    modes = {"402": "http_402", "429": "http_429", "500": "http_500"}
    context.gw.fake.set_mode(context.gw.model_of(dep_id), modes[status])


@step(r"假上游對所有請求回傳 HTTP (\d+)")
def step_fake_all_status(context, status):
    modes = {"402": "http_402", "429": "http_429", "500": "http_500"}
    context.gw.fake.set_mode("*", modes[status])


@step(r"假上游恢復正常且系統時間推進 (\d+) 秒")
def step_fake_reset_advance(context, seconds):
    context.gw.fake.reset()
    context.gw.clock.advance(seconds=int(seconds))


@step(r'假上游恢復正常且系統時間推進至 "([^"]+)"')
def step_fake_reset_set(context, when):
    context.gw.fake.reset()
    context.gw.clock.set(_parse_utc(when))


# ---------------------------------------------------------------- 客戶端請求


@step(r'客戶端以 model "([^"]+)" 送出 (\d+) 次請求')
def step_send(context, model, n):
    gw = context.gw
    for _ in range(int(n)):
        gw.chat(model)
    if gw.last.status_code == 200:
        gw.last_ledger_id = gw.latest_ledger()["id"]


@step(r'客戶端以 model "([^"]+)" 送出 prompt "([^"]*)"')
def step_send_prompt(context, model, prompt):
    gw = context.gw
    context.prompt = prompt
    gw.chat(model, messages=[{"role": "user", "content": prompt}])
    assert gw.last.status_code == 200, gw.last.text
    gw.last_ledger_id = gw.latest_ledger()["id"]


@step(r"回應狀態為 (\d+)")
def step_status(context, status):
    resp = context.gw.last
    assert resp.status_code == int(status), f"{resp.status_code} {resp.text}"


@step(r'回應 header "([^"]+)" 為 "([^"]*)"')
def step_header(context, name, value):
    resp = context.gw.last
    assert resp.status_code == 200, f"{resp.status_code} {resp.text}"
    assert resp.headers.get(name) == value, f"{name}={resp.headers.get(name)!r},期望 {value!r}"


@step(r'錯誤碼為 "([^"]+)"')
def step_error_code(context, code):
    body = context.gw.last.json()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message"}, body   # 9.1.2
    assert body["error"]["code"] == code, body


# ---------------------------------------------------------------- dry-run


@step(r'對 route "([^"]+)" 執行 (\d+) 次 dry-run')
def step_dry_run(context, route, n):
    gw = context.gw
    gw.ledger_count_before = gw.ledger_total()
    gw.plans = []
    for _ in range(int(n)):
        gw.dry_run(route)


@step(r'對 route "([^"]+)" 以 max_tokens (\d+) 執行 1 次 dry-run')
def step_dry_run_max_tokens(context, route, max_tokens):
    gw = context.gw
    gw.ledger_count_before = gw.ledger_total()
    gw.plans = []
    gw.dry_run(route, max_tokens=int(max_tokens))


@step(r'成員 "([^"]+)" 的預估 P50 為 (\d+) 且 P90 為 (\d+)')
def step_estimate_values(context, dep_id, p50, p90):
    gw = context.gw
    est = gw.plan_entry(gw.plans[-1], dep_id)["estimate"]
    assert est is not None
    assert (est["out_p50"], est["out_p90"]) == (int(p50), int(p90)), est


@step(r"帳本列數與 dry-run 之前相同")
def step_ledger_unchanged(context):
    gw = context.gw
    assert gw.ledger_count_before is not None
    assert gw.ledger_total() == gw.ledger_count_before


# ---------------------------------------------------------------- 帳本、冷卻


@step(r'成員 "([^"]+)" 的帳本列狀態為 "([^"]+)"')
def step_member_ledger_status(context, dep_id, status):
    row = context.gw.latest_ledger(deployment=dep_id)
    assert row["status"] == status, row


@step(r'成員 "([^"]+)" 冷卻至 "([^"]+)"')
def step_cooling_until(context, dep_id, when):
    gw = context.gw
    budget = gw.deployment(dep_id)["budget"]
    assert budget is not None and budget["exhausted_until"] == when, budget
    row = gw.dashboard_row(dep_id)
    assert row["cooling"] is True and row["cooling_until"] == when, row


@step(r'成員 "([^"]+)" 冷卻至現在加 (\d+) 秒')
def step_cooling_plus(context, dep_id, seconds):
    gw = context.gw
    expected = (gw.clock.now() + timedelta(seconds=int(seconds))).strftime("%Y-%m-%dT%H:%M:%SZ")
    step_cooling_until(context, dep_id, expected)


@step(r'成員 "([^"]+)" 不在冷卻中')
def step_not_cooling(context, dep_id):
    row = context.gw.dashboard_row(dep_id)
    assert row["cooling"] is False and row["cooling_until"] is None, row

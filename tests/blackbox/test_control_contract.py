"""測試控制介面的黑箱契約測試(CHG-20261107-03 T2;決策 5–7;spec 13;契約清單第 1、2 列)。

經 harness(子行程 serve、HTTP、唯讀資料庫):
- 一般 serve 不暴露控制介面:未開啟時各方法與路徑變形 404,body 同不存在的路徑;開啟時 OpenAPI(若提供)
  不含 `/__test__`;HostOrigin;`no-store`、`nosniff`;CLI 子行程環境不含旋鈕;HOME 條件(結束碼 2、
  不建資料庫)。
- 端點的請求與回應形狀、錯誤碼(404、405、409、422)、時鐘凍結、GET 前後資料庫快照相同、STEPPED tick。
不 import spillway。預設資料目錄一律以子行程的 `HOME`/`XDG_DATA_HOME` 指到暫存目錄後再取得。
"""
from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.blackbox.db import snapshot
from tests.blackbox.fakemods import write_module
from tests.blackbox.harness import (
    CONTROL_ENV, EXIT_USAGE, KNOB_ENVS, TEST_CONTROL_WARNING, child_env, spillway_cmd,
)
from tests.blackbox.mask import TEST_KEY_ENV
from tests.blackbox.ports import free_port
from tests.fakes.cli_upstream import FAKE_MARKER, FAKE_SCRIPT, install

START = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
START_ISO = "2026-11-01T12:00:00Z"
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
OFF_PATHS = ("/__test__", "/__test__/", "/__test__/clock", "/__test__/clock/", "/%5F%5Ftest__/clock",
             "/__test__/retention/tick", "/__test__/raw/deployment-params", "/__test__/no-such")
GET_PATHS = ("/__test__/clock", "/__test__/rng", "/__test__/retention", "/__test__/health-cache",
             "/__test__/cli-slots", "/__test__/mcp/sessions/x")
FAKE_PARAMS_VALUE = "sk-fake-raw-params-0002"
CLAUDE_MEMBER = {
    "id": "claude-sub", "model_name": "claude-sub", "kind": "cli", "provider": "claude-cli",
    "model": "claude-sonnet-5-5", "params": {"max_concurrency": 2, "timeout": 60},
    "capabilities": {"ctx": 1000000, "tools": False, "vision": False, "efforts": ["low", "medium", "high", "max"]},
    "tags": ["t"],
}
API_MEMBER = {
    "id": "api-a", "model_name": "api-a", "kind": "api", "provider": "openai", "model": "gpt-4o-mini",
    "params": {"api_base": "http://127.0.0.1:9/v1"}, "credential_ref": f"env:{TEST_KEY_ENV}",
    "capabilities": {"ctx": 128000, "tools": True, "vision": False, "efforts": []}, "tags": ["chat"],
}
TASK_ROUTE = {"name": "t", "target": {"tags": {"all": ["t"]}}, "mode": "task"}
TASK_BODY = {"model": "t", "messages": [{"role": "user", "content": "do it"}], "metadata": {"mode": "task"}}


def _code(r) -> str:
    return r.json()["error"]["code"]


# ================================================================ 一般 serve 不暴露(13.1)


def test_off_every_method_and_variant_is_404_like_unknown_path(serve_factory):
    s = serve_factory(control=False)
    with s.http() as c:
        for method in METHODS:
            baseline = c.request(method, "/no-such-path-xyz", json={})
            assert baseline.status_code == 404
            assert baseline.json() == {"error": {"code": "not_found", "message": "Not Found"}}
            for path in OFF_PATHS:
                r = c.request(method, path, json={"set": START_ISO})
                assert r.status_code == 404, (method, path)
                assert r.content == baseline.content, (method, path)
        r = c.get("/openapi.json")
        if r.status_code == 200:
            assert "/__test__" not in r.text
    assert TEST_CONTROL_WARNING not in s.stderr_text().splitlines()


def test_on_not_in_openapi_and_headers_and_host_origin(serve_factory):
    s = serve_factory(start_time=START)
    with s.http() as c:
        r = c.get("/openapi.json")
        if r.status_code == 200:   # 13.1.3:有提供時不得含 /__test__
            assert "/__test__" not in r.text
        for path in ("/__test__/clock", "/%5F%5Ftest__/clock"):
            r = c.get(path)
            assert r.status_code == 200, path
            assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
        r = c.post("/__test__/clock", json={"bogus": 1})
        assert r.status_code == 422 and r.headers["cache-control"] == "no-store"
        r = c.get("/__test__/clock", headers={"Host": "evil.example"})
        assert r.status_code == 403 and _code(r) == "host_not_allowed"
        r = c.post("/__test__/clock", json={"advance_seconds": 1}, headers={"Origin": "http://evil.example"})
        assert r.status_code == 403 and _code(r) == "origin_not_allowed"
        assert c.get("/__test__/clock").json()["now"] == START_ISO   # 被拒的請求沒有生效
        r = c.delete("/__test__/clock")
        assert r.status_code == 405 and "code" in r.json()["error"]


def _home_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    env = child_env(tmp_path / "unused-home")
    env["HOME"] = str(tmp_path / "fake-user-home")
    env["XDG_DATA_HOME"] = str(tmp_path / "fake-user-home" / "xdg")
    env.pop("SPILLWAY_HOME")
    r = subprocess.run([sys.executable, "-c", "import platformdirs; print(platformdirs.user_data_dir('spillway'))"],
                       env=env, capture_output=True, text=True, check=True)
    default = Path(r.stdout.strip())
    assert default.resolve().is_relative_to((tmp_path / "fake-user-home").resolve()), default
    return env, default


@pytest.mark.parametrize("case", ["unset", "empty", "default", "default-trailing-slash"])
def test_home_check_exit_2_without_creating_database(tmp_path, case):
    env, default = _home_env(tmp_path)
    env[CONTROL_ENV] = "1"
    if case == "empty":
        env["SPILLWAY_HOME"] = ""
    elif case == "default":
        env["SPILLWAY_HOME"] = str(default)
    elif case == "default-trailing-slash":
        env["SPILLWAY_HOME"] = str(default) + "/"
    r = subprocess.run([*spillway_cmd(), "serve", "--port", str(free_port())], env=env, cwd=str(tmp_path),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == EXIT_USAGE, r.stderr
    assert "Traceback" not in r.stderr and TEST_CONTROL_WARNING not in r.stderr
    assert not default.exists()


def _env_dumping_claude(bin_dir: Path) -> Path:
    """假 `claude`(仍帶 `FAKE_MARKER`):先把看得到的 `SPILLWAY*` 變數名寫到 `$SPILLWAY_HOME/cli_env_names.json`,
    再交給假執行檔本體。"""
    install(bin_dir)
    dump = ("import json, os, pathlib; "
            "pathlib.Path(os.environ['SPILLWAY_HOME'], 'cli_env_names.json').write_text("
            "json.dumps(sorted(k for k in os.environ if k.startswith('SPILLWAY'))))")
    target = bin_dir / "claude"
    target.write_text(
        "#!/bin/sh\n"
        f"{FAKE_MARKER}\n"
        f"{shlex.quote(sys.executable)} -I -c {shlex.quote(dump)}\n"
        f"exec {shlex.quote(sys.executable)} -I {shlex.quote(str(FAKE_SCRIPT))} claude \"$@\"\n",
        encoding="utf-8")
    target.chmod(0o755)
    return bin_dir


def test_cli_subprocess_does_not_inherit_knobs(tmp_path, serve_factory):
    mods = tmp_path / "mods"
    write_module(mods, "knobprobe", providers=("knobprobe",))
    s = serve_factory(fake_bin=_env_dumping_claude(tmp_path / "bin"), start_time=START, rng_seed=1,
                      adapter_paths=[mods], retention_poll_seconds=5, retention_stepped=True, no_token=True,
                      log_level="INFO")
    assert s.request("POST", "/api/deployments", json=CLAUDE_MEMBER).status_code == 201
    assert s.request("POST", "/api/routes", json=TASK_ROUTE).status_code == 201
    r = s.request("POST", "/v1/chat/completions", json=TASK_BODY, timeout=60)
    assert r.status_code == 200, r.text
    assert r.headers["X-Spillway-Served-By"] == "claude-sub"
    names = json.loads((s.home / "cli_env_names.json").read_text(encoding="utf-8"))
    assert "SPILLWAY_HOME" in names   # 記錄確實來自 CLI 子行程
    assert not set(names) & {CONTROL_ENV, *KNOB_ENVS}, names


# ================================================================ 時鐘與亂數(13.4.1、13.4.2)


def test_clock_frozen_set_and_advance(serve_factory):
    s = serve_factory(start_time=START)
    c = s.control
    assert c.clock() == {"controllable": True, "now": START_ISO}
    time.sleep(1.1)
    assert c.clock()["now"] == START_ISO   # 凍結:真實時間經過不改變
    assert c.advance(90) == "2026-11-01T12:01:30Z"
    assert c.set_clock("2026-10-01T00:00:00Z") == "2026-10-01T00:00:00Z"   # 可往前
    assert c.advance(0) == "2026-10-01T00:00:00Z"
    assert c.set_clock(START + timedelta(days=1)) == "2026-11-02T12:00:00Z"


@pytest.mark.parametrize("body", [
    {}, {"set": START_ISO, "advance_seconds": 1}, {"advance_seconds": -1}, {"advance_seconds": "1"},
    {"advance_seconds": 1.5}, {"advance_seconds": True}, {"set": "2026-11-01T12:00:00"},
    {"set": "2026-11-01T12:00:00+00:00"}, {"set": "2026-11-01T12:00:00.5Z"}, {"set": "2026-11-01"},
    {"set": "2026-13-01T00:00:00Z"}, {"set": "２０２６-11-01T12:00:00Z"}, {"set": 0}, {"unknown": 1},
    {"advance_seconds": 2**63},
])
def test_clock_post_422(serve_factory, body):
    s = serve_factory(start_time=START)
    r = s.control.request("POST", "/clock", json=body)
    assert r.status_code == 422 and _code(r) == "validation_error", (body, r.text)
    assert s.control.clock()["now"] == START_ISO


def test_system_clock_mode(serve_factory):
    s = serve_factory()
    body = s.control.clock()
    assert body["controllable"] is False
    now = datetime.strptime(body["now"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs((now - datetime.now(timezone.utc)).total_seconds()) < 120
    for req in ({"advance_seconds": 1}, {"set": START_ISO}):
        r = s.control.request("POST", "/clock", json=req)
        assert r.status_code == 409 and _code(r) == "clock_not_controllable"


def test_rng_seeded_and_unseeded(serve_factory):
    assert serve_factory(rng_seed=-(2**63)).control.rng() == {"seeded": True, "seed": -(2**63)}
    assert serve_factory().control.rng() == {"seeded": False, "seed": None}


# ================================================================ 帳本與內容(13.4.3)


def test_ledger_insert_and_upsert_replaces_whole_row(serve_factory):
    s = serve_factory()
    ids = s.control.ledger([
        {"ts": START_ISO, "status": "ok", "deployment_id": "d1", "tokens_in": 10, "cost_usd": 0.5, "error": None},
        {"id": 50, "ts": START_ISO, "status": "error", "error": "boom", "fallback_depth": 2},
    ])
    assert len(ids) == 2 and ids[1] == 50
    assert s.control.ledger([{"id": 50, "ts": "2026-11-02T00:00:00Z", "status": "ok", "deployment_id": "d2"}]) == [50]
    with s.db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0]
        row = conn.execute("SELECT ts, status, deployment_id, error, fallback_depth FROM usage_ledger "
                           "WHERE id = 50").fetchone()
        first = conn.execute("SELECT tokens_in, cost_usd FROM usage_ledger WHERE id = ?", (ids[0],)).fetchone()
    assert count == 2
    assert row == ("2026-11-02T00:00:00Z", "ok", "d2", None, 0)   # 未給的欄位回到預設值
    assert first == (10, 0.5)


@pytest.mark.parametrize("row", [
    {"status": "ok"}, {"ts": "2026-11-01", "status": "ok"}, {"ts": START_ISO},
    {"ts": START_ISO, "status": "ok", "unknown": 1}, {"ts": START_ISO, "status": "ok", "id": 0},
    {"ts": START_ISO, "status": "ok", "tokens_in": "3"},
])
def test_ledger_422(serve_factory, row):
    s = serve_factory()
    r = s.control.request("POST", "/ledger", json={"rows": [row]})
    assert r.status_code == 422 and _code(r) == "validation_error"


def test_payload_upsert_atomic_batch_and_missing_ledger_422(serve_factory):
    s = serve_factory()
    lid = s.control.ledger([{"ts": START_ISO, "status": "ok"}])[0]
    row = {"ledger_id": lid, "ts": START_ISO, "request_json": "[]", "response_text": "hi", "bytes": 4}
    assert s.control.payloads([row]) == [lid]
    assert s.control.payloads([{**row, "response_text": None, "bytes": 2, "redacted": True}]) == [lid]
    with s.db() as conn:
        p = conn.execute("SELECT response_text, bytes, truncated, redacted FROM usage_payloads "
                         "WHERE ledger_id = ?", (lid,)).fetchone()
    assert p == (None, 2, 0, 1)
    with s.db() as conn:
        before = snapshot(conn)
    r = s.control.request("POST", "/payloads", json={"rows": [{**row, "bytes": 9}, {**row, "ledger_id": lid + 999}]})
    assert r.status_code == 422 and _code(r) == "validation_error"
    with s.db() as conn:
        assert snapshot(conn) == before   # 整批一個交易:任一列失敗時全部不寫入


# ================================================================ 冷卻、損壞狀態(13.4.4、13.4.5)


def test_cooldown_overwrites_even_to_earlier_time(serve_factory):
    s = serve_factory(start_time=START)
    assert s.request("POST", "/api/deployments", json=API_MEMBER).status_code == 201
    later = {"deployment": "api-a", "until": "2026-12-01T00:00:00Z", "reason": "quota_exhausted"}
    assert s.control.cooldown(**later) == later
    earlier = {"deployment": "api-a", "until": "2026-11-05T00:00:00Z", "reason": "rate_limited"}
    assert s.control.cooldown(**earlier) == earlier
    with s.db() as conn:
        assert snapshot(conn)["cooldowns"] == [("api-a", "2026-11-05T00:00:00Z", "rate_limited")]
    dep = s.request("GET", "/api/deployments/api-a").json()
    assert dep["cooling_until"] == "2026-11-05T00:00:00Z" and dep["cooling_reason"] == "rate_limited"
    r = s.control.request("POST", "/cooldowns", json={**later, "deployment": "nope"})
    assert r.status_code == 404 and _code(r) == "not_found"
    for bad in ({**later, "reason": "error"}, {**later, "until": "2026-12-01"}, {"deployment": "api-a"},
                {**later, "deployment": "nope", "reason": "error"}):   # 先檢查格式(422)再檢查存在(404)
        r = s.control.request("POST", "/cooldowns", json=bad)
        assert r.status_code == 422 and _code(r) == "validation_error", bad


def test_raw_params_only_updates_params_and_never_echoes(serve_factory):
    s = serve_factory(log_level="DEBUG")
    assert s.request("POST", "/api/deployments", json=API_MEMBER).status_code == 201
    with s.db() as conn:
        cols = [d[1] for d in conn.execute("PRAGMA table_info(deployments)")]
        before = dict(zip(cols, conn.execute("SELECT * FROM deployments WHERE id = 'api-a'").fetchone()))
    r = s.control.request("POST", "/raw/deployment-params", json={"id": "api-a", "params": {"api_key": FAKE_PARAMS_VALUE}})
    assert r.status_code == 200 and r.json() == {"id": "api-a"} and FAKE_PARAMS_VALUE not in r.text
    with s.db() as conn:
        after = dict(zip(cols, conn.execute("SELECT * FROM deployments WHERE id = 'api-a'").fetchone()))
    assert json.loads(after.pop("params")) == {"api_key": FAKE_PARAMS_VALUE}
    before.pop("params")
    assert after == before
    r = s.control.request("POST", "/raw/deployment-params", json={"id": "nope", "params": {"x": FAKE_PARAMS_VALUE}})
    assert r.status_code == 404 and _code(r) == "not_found" and FAKE_PARAMS_VALUE not in r.text
    r = s.control.request("POST", "/raw/deployment-params", json={"id": "api-a", "params": {}, "extra": FAKE_PARAMS_VALUE})
    assert r.status_code == 422 and FAKE_PARAMS_VALUE not in r.text
    # 損壞的 params 真的生效:匯出時 422
    s.control.raw_deployment_params("api-a", {"api_key": "x"})
    assert s.request("GET", "/api/config/export").status_code == 422
    s.drain_stderr()
    assert FAKE_PARAMS_VALUE.encode() not in s.stderr_bytes()   # DEBUG 日誌也不記錄 body


# ================================================================ 保存清理(13.4.6)


def _seed_old_payload(s, ts: str) -> int:
    lid = s.control.ledger([{"ts": ts, "status": "ok"}])[0]
    s.control.payloads([{"ledger_id": lid, "ts": ts, "request_json": "[]", "response_text": "x", "bytes": 3}])
    return lid


def test_retention_run_and_get(serve_factory):
    s = serve_factory(start_time=START)
    assert s.control.retention() == {"runs": 1, "next_due": "2026-11-01T13:00:00Z"}   # 啟動時先清理一次
    lid = _seed_old_payload(s, "2026-10-01T00:00:00Z")
    assert s.control.retention_run() == 1
    assert s.control.retention()["runs"] == 2
    assert s.request("GET", f"/api/ledger/{lid}/payload").status_code == 410
    r = s.control.request("POST", "/retention/tick", json={"advance_seconds": 0})
    assert r.status_code == 409 and _code(r) == "retention_not_stepped"


def test_stepped_tick_goes_through_due_check(serve_factory):
    """poll 900 秒:到期門檻為 `next_due − 1800`。推進 1799 秒不清理、真實時間經過不清理、再 1 秒才清理。"""
    s = serve_factory(start_time=START, retention_stepped=True, retention_poll_seconds=900)
    lid = _seed_old_payload(s, "2026-10-01T00:00:00Z")
    assert s.control.retention() == {"runs": 1, "next_due": "2026-11-01T13:00:00Z"}
    assert s.control.tick(1799) == {"ran": False, "deleted": None, "runs": 1, "now": "2026-11-01T12:29:59Z"}
    time.sleep(1.0)
    assert s.control.retention()["runs"] == 1
    assert s.control.tick(1) == {"ran": True, "deleted": 1, "runs": 2, "now": "2026-11-01T12:30:00Z"}
    assert s.control.retention()["next_due"] == "2026-11-01T13:30:00Z"   # 清理後 = now + 1 小時
    assert s.request("GET", f"/api/ledger/{lid}/payload").status_code == 410
    for bad in ({"advance_seconds": -1}, {}, {"advance_seconds": "1"}):
        r = s.control.request("POST", "/retention/tick", json=bad)
        assert r.status_code == 422 and _code(r) == "validation_error", bad


def test_stepped_tick_with_system_clock(serve_factory):
    s = serve_factory(retention_stepped=True)
    r = s.control.request("POST", "/retention/tick", json={"advance_seconds": 1})
    assert r.status_code == 409 and _code(r) == "clock_not_controllable"
    assert s.control.tick(0)["runs"] == 1


# ================================================================ 健康快取、CLI 名額、MCP(13.4.7–13.4.9)


def test_health_cache_lists_live_entries_only(serve_factory, fake_api):
    s = serve_factory(start_time=START)
    local = {"id": "loc-1", "model_name": "loc-1", "kind": "local", "provider": "hosted_vllm", "model": "local-model",
             "params": {"api_base": fake_api.base_url + "/v1"},
             "capabilities": {"ctx": 32768, "tools": False, "vision": False, "efforts": []}, "tags": ["l"]}
    assert s.request("POST", "/api/deployments", json=local).status_code == 201
    assert s.request("POST", "/api/routes", json={"name": "l", "target": {"tags": {"any": ["l"]}}}).status_code == 201
    assert s.control.health_cache() == []
    r = s.request("POST", "/v1/chat/completions", json={"model": "l", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200, r.text   # 實際請求才探測 local 健康(dry-run 不探測)
    assert s.control.health_cache() == [{"deployment": "loc-1", "healthy": True}]
    s.control.advance(24 * 3600)   # 遠超過 TTL
    assert s.control.health_cache() == []


def test_cli_slots_acquire_release_limits(serve_factory):
    s = serve_factory()
    assert s.request("POST", "/api/deployments", json=CLAUDE_MEMBER).status_code == 201
    assert s.request("POST", "/api/deployments", json=API_MEMBER).status_code == 201
    c = s.control
    slot = {"deployment": "claude-sub", "in_flight": 0, "limit": 2}
    assert c.cli_slots() == [slot]
    assert c.acquire("claude-sub", 2) == {**slot, "in_flight": 2}
    r = c.request("POST", "/cli-slots/acquire", json={"deployment": "claude-sub", "n": 1})
    assert r.status_code == 409 and _code(r) == "cli_slots_exceeded"
    assert c.cli_slots() == [{**slot, "in_flight": 2}]
    r = c.request("POST", "/cli-slots/release", json={"deployment": "claude-sub", "n": 3})
    assert r.status_code == 409 and _code(r) == "cli_slots_underflow"
    assert c.release("claude-sub", 1) == {**slot, "in_flight": 1}
    r = c.request("POST", "/cli-slots/acquire", json={"deployment": "claude-sub", "n": 2})
    assert r.status_code == 409 and c.cli_slots() == [{**slot, "in_flight": 1}]   # 全有或全無
    for dep in ("nope", "api-a"):
        r = c.request("POST", "/cli-slots/acquire", json={"deployment": dep, "n": 1})
        assert r.status_code == 404 and _code(r) == "not_found"
    for bad in ({"deployment": "claude-sub", "n": 0}, {"deployment": "claude-sub"}):
        assert c.request("POST", "/cli-slots/acquire", json=bad).status_code == 422
    # 佔用的名額與實際 CLI 呼叫共用:路由篩選視為 concurrency_full
    assert s.request("POST", "/api/routes", json=TASK_ROUTE).status_code == 201
    c.acquire("claude-sub", 1)
    plan = s.request("POST", "/api/routes/t/dry-run", json=TASK_BODY).json()
    assert {x["deployment"]: x["reason"] for x in plan["excluded"]}["claude-sub"] == "concurrency_full"


def test_mcp_session_stream_open(serve_factory):
    s = serve_factory()
    assert s.control.stream_open("does-not-exist") is False

    async def body() -> None:
        async with s.mcp_session() as client:
            await s.wait_stream_open(client.session_id)

    asyncio.run(body())


def test_no_token_knob_mcp_401(serve_factory):
    s = serve_factory(no_token=True)
    assert s.token is None and s.request("GET", "/api/settings").json()["token_issued"] is False
    r = s.request("POST", "/mcp/all", json={}, headers={"Accept": "application/json, text/event-stream",
                                                        "Authorization": "Bearer " + "x" * 43})
    assert r.status_code == 401


# ================================================================ GET 不改變資料庫(13.3.3)


def test_get_endpoints_do_not_change_database(serve_factory):
    s = serve_factory(start_time=START, retention_stepped=True)
    assert s.request("POST", "/api/deployments", json=CLAUDE_MEMBER).status_code == 201
    _seed_old_payload(s, "2026-10-01T00:00:00Z")
    with s.db() as conn:
        before = snapshot(conn)
    for path in GET_PATHS:
        assert s.request("GET", path).status_code == 200, path
    with s.db() as conn:
        assert snapshot(conn) == before

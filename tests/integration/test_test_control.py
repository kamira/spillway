"""測試控制介面、啟動期旋鈕與行程接線(CHG-20261107-03 T1;決策 5–7;spec「測試控制介面(僅測試模式)」)。

本檔是 Python 專屬接線的測試(契約清單「旋鈕只在 cli 讀取」);同一契約的黑箱版在
`tests/blackbox/test_control_contract.py`(T2)。

L1(行程內):每個端點的請求與回應形狀、錯誤碼(404、409、422);時鐘凍結;`retention/tick` 在 STEPPED
下真的經過 `_loop`→`due()`→`run_once()`、回到閘前才回應;cli-slots 超過 limit 409;raw 只改 `params`、
不回顯;未開啟時各方法與路徑變形 404 且 body 同不存在路徑;`no-store`;不列入 OpenAPI;HostOrigin;
`create_app` 與 `api/test_control.py` 不讀 `os.environ`;CliRunner 子行程環境不含旋鈕;`SPILLWAY-LOG`
handler 格式;v2 DDL 搬檔後 `MIGRATIONS` 逐字相同。

L2(子行程 `python -m spillway serve`,暫存 SPILLWAY_HOME):CONTROL 與各旋鈕生效(stderr 警告、時鐘、seed、
adapter 路徑、保存輪詢、STEPPED tick、不簽發 token、`SPILLWAY-LOG`);HOME 未設或等於預設目錄時結束碼 2
且未建資料庫;旋鈕值不合法結束碼 2;未設 CONTROL 時只印旋鈕名稱註明已忽略、`/__test__/*` 404;
CLI 子行程不繼承旋鈕。預設資料目錄一律以子行程的 `HOME`/`XDG_DATA_HOME` 指到暫存目錄後再取得,
不碰擁有者的資料目錄。
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import os
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
import pytest

import spillway.api.test_control as test_control_mod
import spillway.app as app_mod
from spillway import cli, settings
from spillway.adapters.base import CliRunner
from spillway.adapters.base.cli_runner import TEST_CONTROL_ENV_NAMES
from spillway.api.http_headers import API_HEADERS, headers_for_path
from spillway.app import TOKEN_NOTICE, RetentionScheduler, create_app
from spillway.clock import SystemClock, from_iso, to_iso
from spillway.store import db as store_db
from spillway.store import repo
from spillway.store.db import connect
from tests.contract.fakemods import write_module
from tests.fakes.cli_upstream import FAKE_MARKER, FAKE_SCRIPT, install, safe_subprocess_env

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
BASE_URL = "http://127.0.0.1:8787"
START = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
START_ISO = "2026-11-01T12:00:00Z"
REAL_KEY_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
FAKE_PARAMS_VALUE = "sk-fake-raw-params-0001"
KNOBS = {
    "SPILLWAY_TEST_START_TIME": START_ISO,
    "SPILLWAY_TEST_RNG_SEED": "424242",
    "SPILLWAY_TEST_RETENTION_POLL_SECONDS": "900",
    "SPILLWAY_TEST_RETENTION_STEPPED": "1",
    "SPILLWAY_TEST_NO_TOKEN": "1",
    "SPILLWAY_TEST_LOG_LEVEL": "DEBUG",
}
CLAUDE_MEMBER = {
    "id": "claude-sub", "model_name": "claude-sub", "kind": "cli", "provider": "claude-cli",
    "model": "claude-sonnet-5-5", "params": {"max_concurrency": 2, "timeout": 60},
    "capabilities": {"ctx": 1000000, "tools": False, "vision": False,
                     "efforts": ["low", "medium", "high", "max"]},
    "tags": ["t"],
}
API_MEMBER = {
    "id": "api-a", "model_name": "api-a", "kind": "api", "provider": "openai", "model": "gpt-4o-mini",
    "params": {"api_base": "http://127.0.0.1:9/v1"}, "credential_ref": "env:SPILLWAY_TEST_KEY",
    "capabilities": {"ctx": 128000, "tools": True, "vision": False, "efforts": []}, "tags": ["chat"],
}
TASK_ROUTE = {"name": "t", "target": {"tags": {"all": ["t"]}}, "mode": "task"}
TASK_BODY = {"model": "t", "messages": [{"role": "user", "content": "do it"}], "metadata": {"mode": "task"}}

# CHG-20261101-03 的 v2 DDL(搬檔前 `db.py` 的 `_SCHEMA_V2` 字面值;AM-4 要求逐字不變)
V2_DDL_SHA256 = "703aa5eaee09a134b7430d302b919be93d28b6c9dc1b0f822a691ed5dfebc5a4"
V2_DDL = """
CREATE TABLE cooldowns (
  deployment_id   TEXT PRIMARY KEY REFERENCES deployments(id) ON DELETE CASCADE,
  exhausted_until TEXT NOT NULL,    -- 上游回報用完或限流時設定
  reason          TEXT NOT NULL CHECK (reason IN ('quota_exhausted','rate_limited'))
);

INSERT INTO cooldowns (deployment_id, exhausted_until, reason)
SELECT b.deployment_id, b.exhausted_until,
       COALESCE((SELECT l.status FROM usage_ledger l
                 WHERE l.deployment_id = b.deployment_id
                   AND l.status IN ('quota_exhausted','rate_limited')
                 ORDER BY l.ts DESC, l.id DESC LIMIT 1), 'quota_exhausted')
FROM budgets b
WHERE b.exhausted_until IS NOT NULL;

ALTER TABLE budgets DROP COLUMN exhausted_until;
"""


# ================================================================ L1:行程內


@asynccontextmanager
async def _open(**kw: Any) -> AsyncIterator[tuple[Any, httpx.AsyncClient]]:
    kw.setdefault("test_control", True)
    app = create_app(db_path=settings.db_path(), **kw)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as c:
            yield app, c


def _db_dump() -> list[str]:
    conn = sqlite3.connect(f"file:{settings.db_path()}?mode=ro", uri=True)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def _err_code(r: httpx.Response) -> str:
    return r.json()["error"]["code"]


# ---------------------------------------------------------------- 只在 cli 讀環境變數(決策 5 ①)


def _environ_reads(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv", "environb"):
            hits.append(f"{path.name}:{node.lineno}")
        elif isinstance(node, ast.Name) and node.id in ("environ", "getenv"):
            hits.append(f"{path.name}:{node.lineno}")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            hits.append(f"{path.name}:{node.lineno}")
    return hits


def test_create_app_and_test_control_module_do_not_read_environ():
    for mod in (app_mod, test_control_mod):
        assert _environ_reads(Path(mod.__file__)) == [], mod.__name__


def test_environ_scanner_detects_reads(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("import os\nx = os.environ.get('A')\ny = os.getenv('B')\nfrom os import environ\n")
    assert len(_environ_reads(p)) == 3


@pytest.mark.anyio
async def test_env_knobs_do_not_affect_create_app(monkeypatch):
    monkeypatch.setenv("SPILLWAY_TEST_CONTROL", "1")
    for name, value in KNOBS.items():
        monkeypatch.setenv(name, value)
    async with _open(test_control=False) as (app, c):
        assert (await c.get("/__test__/clock")).status_code == 404
        assert (await c.get("/__test__/rng")).status_code == 404
        assert isinstance(app.state.clock, SystemClock)
        assert app.state.retention.stepped is False
        assert app.state.retention.poll_seconds == app_mod.DEFAULT_RETENTION_POLL_SECONDS


def test_retention_stepped_requires_test_control():
    with pytest.raises(ValueError):
        create_app(retention_stepped=True)


# ---------------------------------------------------------------- 未開啟時 404(決策 5 ③)

METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")
OFF_PATHS = ("/__test__", "/__test__/", "/__test__/clock", "/__test__/clock/", "/__test__/rng",
             "/__test__/retention/tick", "/__test__/raw/deployment-params", "/__test__/mcp/sessions/x",
             "/%5F%5Ftest__/clock", "/%5f%5ftest__/clock", "/_%5Ftest__/ledger", "/__test%5F%5F/clock")


@pytest.mark.anyio
async def test_unmounted_paths_are_404_with_same_body_as_unknown_path():
    async with _open(test_control=False) as (_app, c):
        for method in METHODS:
            baseline = await c.request(method, "/no-such-path-xyz", json={})
            assert baseline.status_code == 404
            for path in OFF_PATHS:
                r = await c.request(method, path, json={"set": START_ISO})
                assert r.status_code == 404, (method, path)
                assert r.content == baseline.content, (method, path)


@pytest.mark.anyio
async def test_mounted_encoded_paths_reach_router_and_carry_no_store():
    async with _open(start_time=START) as (_app, c):
        for path in ("/__test__/clock", "/%5F%5Ftest__/clock"):
            r = await c.get(path)
            assert r.status_code == 200, path
            for name, value in API_HEADERS:
                assert r.headers[name] == value
        r = await c.post("/__test__/clock", json={"bogus": 1})
        assert r.status_code == 422
        assert r.headers["cache-control"] == "no-store"


def test_headers_for_test_prefix():
    assert headers_for_path("/__test__/clock") == API_HEADERS
    assert headers_for_path("/__test__") == API_HEADERS
    assert headers_for_path("/__test__x") == ()


@pytest.mark.anyio
async def test_not_in_openapi_and_host_origin_applies():
    async with _open(start_time=START) as (app, c):
        assert not [p for p in app.openapi()["paths"] if p.startswith("/__test__")]
        assert (await c.get("/openapi.json")).status_code == 404
        r = await c.get("/__test__/clock", headers={"Host": "evil.example"})
        assert r.status_code == 403 and _err_code(r) == "host_not_allowed"
        r = await c.post("/__test__/clock", json={"advance_seconds": 1},
                         headers={"Origin": "http://evil.example"})
        assert r.status_code == 403 and _err_code(r) == "origin_not_allowed"


# ---------------------------------------------------------------- 時鐘與亂數(決策 7 ①②)


@pytest.mark.anyio
async def test_clock_controllable_frozen_set_and_advance():
    async with _open(start_time=START) as (app, c):
        r = await c.get("/__test__/clock")
        assert r.status_code == 200 and r.json() == {"controllable": True, "now": START_ISO}
        await asyncio.sleep(1.1)
        assert (await c.get("/__test__/clock")).json()["now"] == START_ISO   # 凍結
        r = await c.post("/__test__/clock", json={"advance_seconds": 90})
        assert r.status_code == 200 and r.json() == {"now": "2026-11-01T12:01:30Z"}
        r = await c.post("/__test__/clock", json={"set": "2026-12-31T23:59:59Z"})
        assert r.json() == {"now": "2026-12-31T23:59:59Z"}
        assert to_iso(app.state.clock.now()) == "2026-12-31T23:59:59Z"
        r = await c.post("/__test__/clock", json={"advance_seconds": 0})
        assert r.json() == {"now": "2026-12-31T23:59:59Z"}


@pytest.mark.anyio
@pytest.mark.parametrize("body", [
    {}, {"set": START_ISO, "advance_seconds": 1}, {"advance_seconds": -1}, {"advance_seconds": 1.5},
    {"advance_seconds": "1"}, {"set": "2026-11-01T12:00:00+00:00"}, {"set": "2026-11-01 12:00:00Z"},
    {"set": "2026-13-01T00:00:00Z"}, {"set": "2026-11-01T12:00:00.5Z"}, {"set": 1}, {"other": 1},
])
async def test_clock_post_422(body):
    async with _open(start_time=START) as (_app, c):
        r = await c.post("/__test__/clock", json=body)
        assert r.status_code == 422 and _err_code(r) == "validation_error", body
        assert (await c.get("/__test__/clock")).json()["now"] == START_ISO


@pytest.mark.anyio
async def test_system_clock_mode_get_200_post_409():
    async with _open() as (_app, c):
        r = await c.get("/__test__/clock")
        assert r.status_code == 200
        body = r.json()
        assert body["controllable"] is False
        assert abs(from_iso(body["now"]) - datetime.now(timezone.utc)) < timedelta(seconds=5)
        for payload in ({"advance_seconds": 1}, {"set": START_ISO}):
            r = await c.post("/__test__/clock", json=payload)
            assert r.status_code == 409 and _err_code(r) == "clock_not_controllable"


@pytest.mark.anyio
async def test_rng_seeded_and_selector_uses_same_object():
    async with _open(rng_seed=7) as (app, c):
        assert (await c.get("/__test__/rng")).json() == {"seeded": True, "seed": 7}
        assert app.state.router.rng is app.state.rng
        assert app.state.router.selector._rng is app.state.rng   # 抽選來源就是播種的 rng
        expected = __import__("random").Random(7).random()
        assert app.state.rng.random() == expected
    async with _open() as (_app, c):
        assert (await c.get("/__test__/rng")).json() == {"seeded": False, "seed": None}


# ---------------------------------------------------------------- 帳本與內容(決策 7 ③)


@pytest.mark.anyio
async def test_ledger_insert_and_upsert_replaces_whole_row():
    async with _open() as (_app, c):
        r = await c.post("/__test__/ledger", json={"rows": [
            {"ts": START_ISO, "status": "ok", "deployment_id": "d1", "tokens_in": 10, "cost_usd": 0.5,
             "error": None},
            {"id": 50, "ts": START_ISO, "status": "error", "error": "boom", "fallback_depth": 2},
        ]})
        assert r.status_code == 200, r.text
        ids = r.json()["ids"]
        assert len(ids) == 2 and ids[1] == 50
        r = await c.post("/__test__/ledger", json={"rows": [
            {"id": 50, "ts": "2026-11-02T00:00:00Z", "status": "ok", "deployment_id": "d2"}]})
        assert r.json() == {"ids": [50]}
        conn = connect(settings.db_path())
        try:
            row = repo.get_ledger(conn, 50)
            first = repo.get_ledger(conn, ids[0])
            count = conn.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0]
        finally:
            conn.close()
        assert count == 2
        assert row.status == "ok" and row.deployment_id == "d2" and row.error is None
        assert row.fallback_depth == 0 and row.ts == datetime(2026, 11, 2, tzinfo=timezone.utc)
        assert first.tokens_in == 10 and first.cost_usd == 0.5


@pytest.mark.anyio
@pytest.mark.parametrize("row", [
    {"status": "ok"}, {"ts": "2026-11-01", "status": "ok"}, {"ts": START_ISO},
    {"ts": START_ISO, "status": "ok", "unknown": 1}, {"ts": START_ISO, "status": "ok", "id": 0},
    {"ts": START_ISO, "status": "ok", "tokens_in": "3"},
])
async def test_ledger_422(row):
    async with _open() as (_app, c):
        r = await c.post("/__test__/ledger", json={"rows": [row]})
        assert r.status_code == 422 and _err_code(r) == "validation_error"


@pytest.mark.anyio
async def test_payload_upsert_and_missing_ledger_422():
    async with _open() as (_app, c):
        lid = (await c.post("/__test__/ledger", json={"rows": [{"ts": START_ISO, "status": "ok"}]})).json()["ids"][0]
        row = {"ledger_id": lid, "ts": START_ISO, "request_json": "[]", "response_text": "hi", "bytes": 4}
        r = await c.post("/__test__/payloads", json={"rows": [row]})
        assert r.status_code == 200 and r.json() == {"ids": [lid]}
        r = await c.post("/__test__/payloads", json={"rows": [{**row, "response_text": None, "bytes": 2,
                                                               "redacted": True}]})
        assert r.json() == {"ids": [lid]}
        conn = connect(settings.db_path())
        try:
            p = repo.get_payload(conn, lid)
        finally:
            conn.close()
        assert p.response_text is None and p.bytes == 2 and p.redacted is True and p.truncated is False
        r = await c.post("/__test__/payloads", json={"rows": [{**row, "ledger_id": lid + 999}]})
        assert r.status_code == 422 and _err_code(r) == "validation_error"
        r = await c.post("/__test__/payloads", json={"rows": [{**row, "ts": "bad"}]})
        assert r.status_code == 422


# ---------------------------------------------------------------- 冷卻與損壞狀態(決策 7 ④⑤)


@pytest.mark.anyio
async def test_cooldown_overwrites_even_to_earlier_time():
    async with _open() as (_app, c):
        assert (await c.post("/api/deployments", json=API_MEMBER)).status_code == 201
        later = {"deployment": "api-a", "until": "2026-12-01T00:00:00Z", "reason": "quota_exhausted"}
        r = await c.post("/__test__/cooldowns", json=later)
        assert r.status_code == 200 and r.json() == later
        earlier = {"deployment": "api-a", "until": "2026-11-05T00:00:00Z", "reason": "rate_limited"}
        assert (await c.post("/__test__/cooldowns", json=earlier)).json() == earlier
        conn = connect(settings.db_path())
        try:
            cd = repo.get_cooldown(conn, "api-a")
        finally:
            conn.close()
        assert cd.until == datetime(2026, 11, 5, tzinfo=timezone.utc) and cd.reason == "rate_limited"
        r = await c.post("/__test__/cooldowns", json={**later, "deployment": "nope"})
        assert r.status_code == 404 and _err_code(r) == "not_found"
        for bad in ({**later, "reason": "error"}, {**later, "until": "2026-12-01"}, {"deployment": "api-a"}):
            r = await c.post("/__test__/cooldowns", json=bad)
            assert r.status_code == 422 and _err_code(r) == "validation_error"


@pytest.mark.anyio
async def test_raw_params_only_updates_params_and_never_echoes(caplog):
    caplog.set_level(logging.DEBUG)
    async with _open() as (_app, c):
        assert (await c.post("/api/deployments", json=API_MEMBER)).status_code == 201
        conn = connect(settings.db_path())
        try:
            before = dict(conn.execute("SELECT * FROM deployments WHERE id = 'api-a'").fetchone())
        finally:
            conn.close()
        r = await c.post("/__test__/raw/deployment-params",
                         json={"id": "api-a", "params": {"api_key": FAKE_PARAMS_VALUE}})
        assert r.status_code == 200 and r.json() == {"id": "api-a"}
        assert FAKE_PARAMS_VALUE not in r.text
        conn = connect(settings.db_path())
        try:
            after = dict(conn.execute("SELECT * FROM deployments WHERE id = 'api-a'").fetchone())
        finally:
            conn.close()
        assert json.loads(after.pop("params")) == {"api_key": FAKE_PARAMS_VALUE}
        before.pop("params")
        assert after == before
        r = await c.post("/__test__/raw/deployment-params", json={"id": "nope", "params": {"x": FAKE_PARAMS_VALUE}})
        assert r.status_code == 404 and _err_code(r) == "not_found" and FAKE_PARAMS_VALUE not in r.text
        r = await c.post("/__test__/raw/deployment-params", json={"id": "api-a", "params": {},
                                                                 "extra": FAKE_PARAMS_VALUE})
        assert r.status_code == 422 and FAKE_PARAMS_VALUE not in r.text
        # 損壞的 params 真的生效:匯出時 422(與 ui/test_settings 的用途相同)
        r = await c.post("/__test__/raw/deployment-params", json={"id": "api-a", "params": {"api_key": "x"}})
        assert (await c.get("/api/config/export")).status_code == 422
    assert FAKE_PARAMS_VALUE not in caplog.text


# ---------------------------------------------------------------- 保存清理(決策 7 ⑥)


async def _seed_old_payload(c: httpx.AsyncClient, ts: str) -> int:
    lid = (await c.post("/__test__/ledger", json={"rows": [{"ts": ts, "status": "ok"}]})).json()["ids"][0]
    r = await c.post("/__test__/payloads", json={"rows": [
        {"ledger_id": lid, "ts": ts, "request_json": "[]", "response_text": "x", "bytes": 3}]})
    assert r.status_code == 200
    return lid


@pytest.mark.anyio
async def test_retention_run_and_get():
    async with _open(start_time=START) as (app, c):
        r = await c.get("/__test__/retention")
        assert r.json() == {"runs": 1, "next_due": "2026-11-01T13:00:00Z"}   # 啟動時先清理一次
        await _seed_old_payload(c, "2026-10-01T00:00:00Z")
        r = await c.post("/__test__/retention/run")
        assert r.status_code == 200 and r.json() == {"deleted": 1}
        assert (await c.get("/__test__/retention")).json()["runs"] == 2

        def broken() -> sqlite3.Connection:
            raise sqlite3.OperationalError("boom")

        app.state.retention._factory = broken
        assert (await c.post("/__test__/retention/run")).json() == {"deleted": None}
        assert (await c.get("/__test__/retention")).json()["runs"] == 2


@pytest.mark.anyio
async def test_tick_409_when_not_stepped():
    async with _open(start_time=START) as (_app, c):
        r = await c.post("/__test__/retention/tick", json={"advance_seconds": 0})
        assert r.status_code == 409 and _err_code(r) == "retention_not_stepped"
        assert (await c.get("/__test__/clock")).json()["now"] == START_ISO   # 沒有推進


@pytest.mark.anyio
async def test_tick_goes_through_real_loop_due_run_once_and_returns_at_gate():
    async with _open(start_time=START, retention_stepped=True, retention_poll_seconds=60) as (app, c):
        retention: RetentionScheduler = app.state.retention
        calls: list[tuple[str, bool]] = []
        real_due, real_run = retention.due, retention.run_once

        def due() -> bool:
            calls.append(("due", asyncio.current_task() is retention.task))
            return real_due()

        def run_once() -> int | None:
            calls.append(("run_once", asyncio.current_task() is retention.task))
            return real_run()

        retention.due = due            # type: ignore[method-assign]
        retention.run_once = run_once  # type: ignore[method-assign]
        await _seed_old_payload(c, "2026-10-01T00:00:00Z")

        # 未到期(next_due 13:00、poll 60 → 門檻 12:58):_loop 跑了 due() 但沒有清理
        r = await c.post("/__test__/retention/tick", json={"advance_seconds": 0})
        assert r.status_code == 200
        assert r.json() == {"ran": False, "deleted": None, "runs": 1, "now": START_ISO}
        assert calls == [("due", True)]
        assert retention._parked.is_set()   # 回應時 _loop 已回到閘前

        # 推進到門檻(now >= next_due − 2×poll):同一輪 due() → run_once(),都在 _loop task 內
        calls.clear()
        r = await c.post("/__test__/retention/tick", json={"advance_seconds": 3600 - 120})
        assert r.json() == {"ran": True, "deleted": 1, "runs": 2, "now": "2026-11-01T12:58:00Z"}
        assert calls == [("due", True), ("run_once", True)]
        assert retention._parked.is_set()
        assert (await c.get("/__test__/retention")).json() == {"runs": 2, "next_due": "2026-11-01T13:58:00Z"}

        # 沒有 tick 時 _loop 不會自己跑(閘取代 sleep)
        calls.clear()
        await asyncio.sleep(0.3)
        assert calls == []

        # body 不合法 422
        for bad in ({"advance_seconds": -1}, {}, {"advance_seconds": 1, "x": 1}):
            assert (await c.post("/__test__/retention/tick", json=bad)).status_code == 422


@pytest.mark.anyio
async def test_tick_with_system_clock_advance_409_zero_ok():
    async with _open(retention_stepped=True) as (_app, c):
        r = await c.post("/__test__/retention/tick", json={"advance_seconds": 5})
        assert r.status_code == 409 and _err_code(r) == "clock_not_controllable"
        r = await c.post("/__test__/retention/tick", json={"advance_seconds": 0})
        assert r.status_code == 200 and r.json()["ran"] is False


@pytest.mark.anyio
async def test_tick_concurrent_requests_are_serialized():
    async with _open(start_time=START, retention_stepped=True) as (_app, c):
        rs = await asyncio.gather(*(c.post("/__test__/retention/tick", json={"advance_seconds": 3600})
                                    for _ in range(3)))
        assert [r.status_code for r in rs] == [200, 200, 200]
        assert sorted(r.json()["runs"] for r in rs) == [2, 3, 4]


# ---------------------------------------------------------------- 健康快取、CLI 名額、MCP(決策 7 ⑦⑧⑨)


@pytest.mark.anyio
async def test_health_cache_lists_only_live_entries():
    async with _open(start_time=START) as (app, c):
        cache = app.state.router.health_cache
        cache.put("old", False)
        await c.post("/__test__/clock", json={"advance_seconds": 3600 * 24})
        cache.put("b-new", True)
        cache.put("a-new", False)
        r = await c.get("/__test__/health-cache")
        assert r.status_code == 200
        assert r.json() == {"entries": [{"deployment": "a-new", "healthy": False},
                                        {"deployment": "b-new", "healthy": True}]}


@pytest.mark.anyio
async def test_cli_slots_acquire_release_limits():
    async with _open() as (app, c):
        assert (await c.post("/api/deployments", json=CLAUDE_MEMBER)).status_code == 201
        assert (await c.post("/api/deployments", json=API_MEMBER)).status_code == 201
        slot = {"deployment": "claude-sub", "in_flight": 0, "limit": 2}
        assert (await c.get("/__test__/cli-slots")).json() == {"slots": [slot]}
        r = await c.post("/__test__/cli-slots/acquire", json={"deployment": "claude-sub", "n": 2})
        assert r.status_code == 200 and r.json() == {**slot, "in_flight": 2}
        assert app.state.cli_runner.in_flight("claude-sub") == 2
        r = await c.post("/__test__/cli-slots/acquire", json={"deployment": "claude-sub", "n": 1})
        assert r.status_code == 409 and _err_code(r) == "cli_slots_exceeded"
        assert app.state.cli_runner.in_flight("claude-sub") == 2
        r = await c.post("/__test__/cli-slots/release", json={"deployment": "claude-sub", "n": 3})
        assert r.status_code == 409 and _err_code(r) == "cli_slots_underflow"
        r = await c.post("/__test__/cli-slots/release", json={"deployment": "claude-sub", "n": 1})
        assert r.json() == {**slot, "in_flight": 1}
        r = await c.post("/__test__/cli-slots/acquire", json={"deployment": "claude-sub", "n": 2})
        assert r.status_code == 409 and app.state.cli_runner.in_flight("claude-sub") == 1   # 全有或全無
        for dep in ("nope", "api-a"):
            r = await c.post("/__test__/cli-slots/acquire", json={"deployment": dep, "n": 1})
            assert r.status_code == 404 and _err_code(r) == "not_found"
        for bad in ({"deployment": "claude-sub", "n": 0}, {"deployment": "claude-sub"}):
            assert (await c.post("/__test__/cli-slots/acquire", json=bad)).status_code == 422
        # dry-run 看得到佔用的名額
        assert (await c.post("/api/routes", json=TASK_ROUTE)).status_code == 201
        await c.post("/__test__/cli-slots/acquire", json={"deployment": "claude-sub", "n": 1})
        plan = (await c.post("/api/routes/t/dry-run", json=TASK_BODY)).json()
        assert {x["deployment"]: x["reason"] for x in plan["excluded"]}["claude-sub"] == "concurrency_full"


@pytest.mark.anyio
async def test_mcp_session_unknown_is_not_open():
    async with _open() as (_app, c):
        r = await c.get("/__test__/mcp/sessions/does-not-exist")
        assert r.status_code == 200 and r.json() == {"stream_open": False}


@pytest.mark.anyio
async def test_get_endpoints_do_not_change_database():
    async with _open(start_time=START, retention_stepped=True) as (_app, c):
        assert (await c.post("/api/deployments", json=CLAUDE_MEMBER)).status_code == 201
        await _seed_old_payload(c, "2026-10-01T00:00:00Z")
        before = _db_dump()
        for path in ("/__test__/clock", "/__test__/rng", "/__test__/retention", "/__test__/health-cache",
                     "/__test__/cli-slots", "/__test__/mcp/sessions/x"):
            assert (await c.get(path)).status_code == 200, path
        assert _db_dump() == before


# ---------------------------------------------------------------- CliRunner 子行程環境(決策 5 ⑦)


@pytest.mark.anyio
async def test_cli_runner_strips_test_control_env(tmp_path, monkeypatch):
    assert TEST_CONTROL_ENV_NAMES[0] == "SPILLWAY_TEST_CONTROL"
    assert set(TEST_CONTROL_ENV_NAMES[1:]) == set(KNOBS) | {"SPILLWAY_TEST_ADAPTER_PATHS"}
    monkeypatch.setenv("SPILLWAY_TEST_CONTROL", "1")
    for name, value in KNOBS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SPILLWAY_TEST_ADAPTER_PATHS", str(tmp_path))
    monkeypatch.setenv("SPILLWAY_TEST_GRACEFUL_TIMEOUT", "3")   # 有意的例外:不在移除清單
    code = "import json, os; print(json.dumps(sorted(k for k in os.environ if k.startswith('SPILLWAY_TEST'))))"
    out = await CliRunner().run([sys.executable, "-I", "-c", code], cwd=tmp_path, timeout=30)
    assert json.loads(out.stdout) == ["SPILLWAY_TEST_GRACEFUL_TIMEOUT"]


# ---------------------------------------------------------------- cli 的旋鈕解析與 HOME 檢查(決策 5 ②、6)


def test_read_test_control_off_prints_only_names(capsys):
    env = {"SPILLWAY_TEST_START_TIME": "secret-value-1", "SPILLWAY_TEST_NO_TOKEN": "secret-value-2",
           "SPILLWAY_TEST_KEY": "sk-test-0000", "SPILLWAY_TEST_GRACEFUL_TIMEOUT": "2"}
    assert cli._read_test_control(env) is None
    err = capsys.readouterr().err
    assert err == cli.TEST_KNOBS_IGNORED + "SPILLWAY_TEST_START_TIME, SPILLWAY_TEST_NO_TOKEN\n"
    assert cli._read_test_control({"SPILLWAY_TEST_CONTROL": "true", "SPILLWAY_TEST_RNG_SEED": "1"}) is None
    assert capsys.readouterr().err == cli.TEST_KNOBS_IGNORED + "SPILLWAY_TEST_RNG_SEED\n"
    assert cli._read_test_control({}) is None
    assert capsys.readouterr().err == ""


def test_read_test_control_on_parses_knobs(tmp_path):
    env = {"SPILLWAY_TEST_CONTROL": "1", **KNOBS,
           "SPILLWAY_TEST_ADAPTER_PATHS": os.pathsep.join([str(tmp_path / "a"), "", str(tmp_path / "b")])}
    ctl = cli._read_test_control(env)
    assert ctl == cli._TestControl(start_time=START, rng_seed=424242,
                                   adapter_paths=(str(tmp_path / "a"), str(tmp_path / "b")),
                                   retention_poll_seconds=900.0, retention_stepped=True, no_token=True,
                                   log_level="DEBUG")
    assert cli._read_test_control({"SPILLWAY_TEST_CONTROL": "1"}) == cli._TestControl()


@pytest.mark.parametrize("name,value", [
    ("SPILLWAY_TEST_START_TIME", "2026-11-01T12:00:00+00:00"),
    ("SPILLWAY_TEST_RNG_SEED", "1.5"),
    ("SPILLWAY_TEST_RETENTION_POLL_SECONDS", "0"),
    ("SPILLWAY_TEST_RETENTION_POLL_SECONDS", "nan"),
    ("SPILLWAY_TEST_RETENTION_POLL_SECONDS", "x"),
    ("SPILLWAY_TEST_RETENTION_STEPPED", "yes"),
    ("SPILLWAY_TEST_NO_TOKEN", "0"),
    ("SPILLWAY_TEST_LOG_LEVEL", "debug"),
])
def test_read_test_control_invalid_value_message_has_no_value(name, value):
    with pytest.raises(cli._KnobError) as ei:
        cli._read_test_control({"SPILLWAY_TEST_CONTROL": "1", name: value})
    assert name in str(ei.value)
    assert value not in str(ei.value)


def test_test_home_problem(tmp_path, monkeypatch):
    default = tmp_path / "default-data" / "spillway"
    monkeypatch.setattr(cli.platformdirs, "user_data_dir", lambda appname: str(default))
    assert cli._test_home_problem({}) is not None
    assert cli._test_home_problem({"SPILLWAY_HOME": ""}) is not None
    assert cli._test_home_problem({"SPILLWAY_HOME": str(default)}) is not None
    assert cli._test_home_problem({"SPILLWAY_HOME": str(default / "x" / "..")}) is not None
    assert cli._test_home_problem({"SPILLWAY_HOME": str(tmp_path / "home")}) is None


def test_run_server_home_check_before_preflight(tmp_path, monkeypatch, capsys):
    seen: list[Any] = []
    monkeypatch.setattr(cli, "_preflight", lambda *a: seen.append(a) or 1)
    monkeypatch.setenv("SPILLWAY_TEST_CONTROL", "1")
    monkeypatch.delenv("SPILLWAY_HOME")
    assert cli._run_server("127.0.0.1", 1) == cli.EXIT_USAGE
    assert seen == []
    monkeypatch.setenv("SPILLWAY_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("SPILLWAY_TEST_RNG_SEED", "nope")
    assert cli._run_server("127.0.0.1", 1) == cli.EXIT_USAGE
    assert seen == [] and "nope" not in capsys.readouterr().err
    monkeypatch.delenv("SPILLWAY_TEST_RNG_SEED")
    assert cli._run_server("127.0.0.1", 1) == 1     # 通過檢查後才做前置檢查
    assert len(seen) == 1
    assert cli.TEST_CONTROL_WARNING + "\n" in capsys.readouterr().err


def test_run_server_passes_knobs_to_create_app(tmp_path, monkeypatch):
    seen: dict[str, Any] = {}

    class _Stop(Exception):
        pass

    def fake_create_app(**kw: Any) -> Any:
        seen.update(kw)
        raise _Stop

    monkeypatch.setattr(cli, "_preflight", lambda *a: 0)
    monkeypatch.setattr(app_mod, "create_app", fake_create_app)
    monkeypatch.setenv("SPILLWAY_TEST_CONTROL", "1")
    for name, value in KNOBS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SPILLWAY_TEST_ADAPTER_PATHS", str(tmp_path / "mods"))
    with pytest.raises(_Stop):
        cli._run_server("127.0.0.1", 9999)
    assert seen == {"issue_token": False, "port": 9999, "test_control": True, "start_time": START,
                    "rng_seed": 424242, "extra_adapter_paths": (str(tmp_path / "mods"),),
                    "retention_stepped": True, "retention_poll_seconds": 900.0}
    seen.clear()
    for name in (*KNOBS, "SPILLWAY_TEST_ADAPTER_PATHS", "SPILLWAY_TEST_CONTROL"):
        monkeypatch.delenv(name)
    with pytest.raises(_Stop):
        cli._run_server("127.0.0.1", 9999)
    assert seen == {"issue_token": True, "port": 9999}   # 一般 serve 的參數不變


def test_log_formatter_lines():
    fmt = cli._TestLogFormatter()
    rec = logging.LogRecord("spillway.x", logging.INFO, __file__, 1, "a %s b\nsecond", ("X",), None)
    assert fmt.format(rec) == "SPILLWAY-LOG INFO spillway.x a X b\nSPILLWAY-LOG-CONT second"
    try:
        raise ValueError("bad\nthing")
    except ValueError:
        rec = logging.LogRecord("n", logging.ERROR, __file__, 1, "oops", (), sys.exc_info())
    lines = fmt.format(rec).split("\n")
    assert lines[0] == "SPILLWAY-LOG ERROR n oops"
    assert lines[1] == "SPILLWAY-LOG-CONT Traceback (most recent call last):"
    assert lines[-2:] == ["SPILLWAY-LOG-CONT ValueError: bad", "SPILLWAY-LOG-CONT thing"]
    assert all(ln.startswith("SPILLWAY-LOG-CONT ") for ln in lines[1:])
    rec = logging.LogRecord("n", logging.DEBUG, __file__, 1, "s", (), None, sinfo="Stack (most recent call last):\n  f")
    assert fmt.format(rec).split("\n") == ["SPILLWAY-LOG DEBUG n s", "SPILLWAY-LOG-CONT Stack (most recent call last):",
                                           "SPILLWAY-LOG-CONT   f"]
    assert fmt.format(logging.LogRecord("n", logging.INFO, __file__, 1, "", (), None)) == "SPILLWAY-LOG INFO n "


def test_install_test_log_handler(capsys):
    root = logging.getLogger()
    old_level = root.level
    handler = cli._install_test_log_handler("DEBUG")
    try:
        assert handler.stream is sys.stderr and handler.level == logging.DEBUG and root.level == logging.DEBUG
        assert not handler.filters
        logging.getLogger("spillway.t1probe").debug("hello %d", 5)
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    assert "SPILLWAY-LOG DEBUG spillway.t1probe hello 5\n" in capsys.readouterr().err


# ---------------------------------------------------------------- v2 DDL 搬檔(AM-4)


def test_v2_ddl_moved_verbatim():
    packaged = resources.files("spillway.store").joinpath("migrations").joinpath("0002.sql")
    assert packaged.read_text(encoding="utf-8") == V2_DDL
    assert hashlib.sha256(V2_DDL.encode("utf-8")).hexdigest() == V2_DDL_SHA256
    assert store_db.MIGRATIONS[1] == (2, V2_DDL)
    assert store_db.MIGRATIONS[0][1] == resources.files("spillway.store").joinpath("schema.sql").read_text(
        encoding="utf-8")
    assert store_db.SCHEMA_VERSION == 2


def test_migrations_sql_is_package_data():
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"**/*.sql"' in text   # store/migrations/0002.sql 與 schema.sql 同一規則打包


# ================================================================ L2:子行程 serve


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _base_env(tmp_path: Path, fake_bin: Path | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in REAL_KEY_VARS and k not in TEST_CONTROL_ENV_NAMES}
    # 預設資料目錄(platformdirs)改指到暫存目錄:即使檢查有錯也不會碰到擁有者的資料
    env["HOME"] = str(tmp_path / "fake-user-home")
    env["XDG_DATA_HOME"] = str(tmp_path / "fake-user-home" / "xdg")
    env["PYTHONPATH"] = str(SRC)
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    return safe_subprocess_env(env, fake_bin=fake_bin)


def _default_data_dir(env: dict[str, str]) -> Path:
    r = subprocess.run([sys.executable, "-c", "import platformdirs; print(platformdirs.user_data_dir('spillway'))"],
                       env=env, capture_output=True, text=True, check=True)
    path = Path(r.stdout.strip())
    assert path.resolve().is_relative_to(Path(env["HOME"]).resolve()), path   # 一定在暫存目錄
    return path


class _Serve:
    def __init__(self, env: dict[str, str], log: Path) -> None:
        self.port = _free_port()
        self.log = log
        self._fh = log.open("wb")
        self.proc = subprocess.Popen([sys.executable, "-m", "spillway", "serve", "--port", str(self.port)],
                                     env=env, cwd=str(REPO_ROOT), stdout=subprocess.DEVNULL,
                                     stderr=self._fh, start_new_session=True)
        self.url = f"http://127.0.0.1:{self.port}"

    def stderr(self) -> str:
        if not self._fh.closed:
            self._fh.flush()
        return self.log.read_text(encoding="utf-8", errors="replace")

    def wait_ready(self, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                pytest.fail(f"serve 提前結束 rc={self.proc.returncode}\n{self.stderr()}")
            try:
                if httpx.get(f"{self.url}/api/settings/context", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        pytest.fail(f"serve 未就緒\n{self.stderr()}")

    def stop(self) -> int:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()
        self._fh.close()
        return self.proc.returncode


@contextmanager
def _serve(env: dict[str, str], tmp_path: Path) -> Iterator[_Serve]:
    s = _Serve(env, tmp_path / f"serve-{time.monotonic_ns()}.log")
    try:
        s.wait_ready()
        yield s
    finally:
        rc = s.stop()
        assert rc == -signal.SIGTERM, s.stderr()
        assert "Traceback" not in s.stderr()


def _run_until_exit(env: dict[str, str], timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "spillway", "serve", "--port", str(_free_port())],
                          env=env, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=timeout)


def test_l2_control_on_all_knobs(tmp_path):
    home = tmp_path / "home"
    mods = tmp_path / "mods"
    write_module(mods, "t1_probe_mod", providers=("t1probe",))
    env = _base_env(tmp_path)
    env.update({"SPILLWAY_HOME": str(home), "SPILLWAY_TEST_CONTROL": "1", **KNOBS,
                "SPILLWAY_TEST_ADAPTER_PATHS": str(mods)})
    with _serve(env, tmp_path) as s:
        c = httpx.Client(base_url=s.url, timeout=30)
        err_lines = s.stderr().splitlines()
        assert cli.TEST_CONTROL_WARNING in err_lines
        assert not [ln for ln in err_lines if ln.startswith(TOKEN_NOTICE)]   # NO_TOKEN
        assert c.get("/api/settings").json()["token_issued"] is False
        assert c.get("/__test__/clock").json() == {"controllable": True, "now": START_ISO}
        assert c.get("/__test__/rng").json() == {"seeded": True, "seed": 424242}
        providers = {p for m in c.get("/api/adapters").json()["modules"] for p in m["providers"]}
        assert "t1probe" in providers
        r = c.get("/__test__/clock")
        assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
        # STEPPED + poll 900:門檻為 next_due − 1800 秒。推進 1800 秒剛好到期(預設 poll 30 時不會到期)
        lid = c.post("/__test__/ledger", json={"rows": [{"ts": "2026-10-01T00:00:00Z", "status": "ok"}]}).json()["ids"][0]
        assert c.post("/__test__/payloads", json={"rows": [{"ledger_id": lid, "ts": "2026-10-01T00:00:00Z",
                                                            "request_json": "[]", "bytes": 2}]}).status_code == 200
        assert c.get("/__test__/retention").json() == {"runs": 1, "next_due": "2026-11-01T13:00:00Z"}
        r = c.post("/__test__/retention/tick", json={"advance_seconds": 1799})
        assert r.json() == {"ran": False, "deleted": None, "runs": 1, "now": "2026-11-01T12:29:59Z"}
        time.sleep(1.0)   # 真實時間經過也不會自己跑
        assert c.get("/__test__/retention").json()["runs"] == 1
        r = c.post("/__test__/retention/tick", json={"advance_seconds": 1})
        assert r.json() == {"ran": True, "deleted": 1, "runs": 2, "now": "2026-11-01T12:30:00Z"}
        assert c.get(f"/api/ledger/{lid}/payload").status_code == 410
        c.close()
        time.sleep(0.2)
        err = s.stderr()
    log_lines = [ln for ln in err.splitlines() if ln.startswith("SPILLWAY-LOG ")]
    assert any(ln.startswith("SPILLWAY-LOG DEBUG ") for ln in log_lines), err[-2000:]
    assert all(len(ln.split(" ", 3)) >= 3 for ln in log_lines)


def test_l2_control_on_without_knobs_uses_system_clock_and_issues_token(tmp_path):
    env = _base_env(tmp_path)
    env.update({"SPILLWAY_HOME": str(tmp_path / "home"), "SPILLWAY_TEST_CONTROL": "1"})
    with _serve(env, tmp_path) as s:
        c = httpx.Client(base_url=s.url, timeout=30)
        body = c.get("/__test__/clock").json()
        assert body["controllable"] is False
        assert c.post("/__test__/clock", json={"advance_seconds": 1}).status_code == 409
        assert c.get("/__test__/rng").json() == {"seeded": False, "seed": None}
        assert c.post("/__test__/retention/tick", json={"advance_seconds": 0}).status_code == 409
        assert c.get("/api/settings").json()["token_issued"] is True
        c.close()
        lines = s.stderr().splitlines()
    assert cli.TEST_CONTROL_WARNING in lines
    tokens = [ln[len(TOKEN_NOTICE):] for ln in lines if ln.startswith(TOKEN_NOTICE)]
    assert len(tokens) == 1 and len(tokens[0]) == 43
    assert not [ln for ln in lines if ln.startswith("SPILLWAY-LOG")]   # 未設 LOG_LEVEL


def test_l2_control_off_ignores_knobs_and_hides_interface(tmp_path):
    env = _base_env(tmp_path)
    env.update({"SPILLWAY_HOME": str(tmp_path / "home"), **KNOBS})
    with _serve(env, tmp_path) as s:
        c = httpx.Client(base_url=s.url, timeout=30)
        baseline = c.get("/no-such-path")
        for method in METHODS:
            for path in ("/__test__/clock", "/__test__/clock/", "/%5F%5Ftest__/clock", "/__test__/"):
                r = c.request(method, path, json={})
                assert r.status_code == 404 and r.content == baseline.content, (method, path)
        assert c.get("/api/settings").json()["token_issued"] is True   # NO_TOKEN 被忽略
        c.close()
        lines = s.stderr().splitlines()
    ignored = [ln for ln in lines if ln.startswith(cli.TEST_KNOBS_IGNORED)]
    assert ignored == [cli.TEST_KNOBS_IGNORED + ", ".join(n for n in TEST_CONTROL_ENV_NAMES[1:] if n in KNOBS)]
    assert cli.TEST_CONTROL_WARNING not in lines
    assert START_ISO not in "\n".join(lines) and "424242" not in "\n".join(lines)   # 不印值
    assert not [ln for ln in lines if ln.startswith("SPILLWAY-LOG")]


@pytest.mark.parametrize("case", ["unset", "empty", "default", "default-trailing"])
def test_l2_home_check_exit_2_without_creating_database(tmp_path, case):
    env = _base_env(tmp_path)
    default = _default_data_dir(env)
    env["SPILLWAY_TEST_CONTROL"] = "1"
    env.pop("SPILLWAY_HOME", None)
    if case == "empty":
        env["SPILLWAY_HOME"] = ""
    elif case == "default":
        env["SPILLWAY_HOME"] = str(default)
    elif case == "default-trailing":
        env["SPILLWAY_HOME"] = str(default) + "/"
    r = _run_until_exit(env)
    assert r.returncode == cli.EXIT_USAGE, r.stderr
    assert "SPILLWAY_HOME" in r.stderr and "Traceback" not in r.stderr
    assert cli.TEST_CONTROL_WARNING not in r.stderr
    assert not default.exists()   # 沒有建資料目錄、沒有資料庫


@pytest.mark.parametrize("name,value", [("SPILLWAY_TEST_START_TIME", "tomorrow"),
                                        ("SPILLWAY_TEST_LOG_LEVEL", "TRACE")])
def test_l2_invalid_knob_exit_2(tmp_path, name, value):
    home = tmp_path / "home"
    env = _base_env(tmp_path)
    env.update({"SPILLWAY_HOME": str(home), "SPILLWAY_TEST_CONTROL": "1", name: value})
    r = _run_until_exit(env)
    assert r.returncode == cli.EXIT_USAGE, r.stderr
    assert name in r.stderr and value not in r.stderr
    assert not (home / "spillway.db").exists()


def _env_dumping_claude(bin_dir: Path) -> Path:
    """假 `claude`(仍是 `install()` 的假執行檔):先把子行程看得到的 `SPILLWAY*` 變數名寫到
    `$SPILLWAY_HOME/cli_env_names.json`,再交給 `tests/fakes/cli_upstream/fake.py`。"""
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


def test_l2_cli_subprocess_does_not_inherit_knobs(tmp_path):
    home = tmp_path / "home"
    bin_dir = _env_dumping_claude(tmp_path / "bin")
    env = _base_env(tmp_path, fake_bin=bin_dir)
    env.update({"SPILLWAY_HOME": str(home), "SPILLWAY_TEST_CONTROL": "1",
                "SPILLWAY_TEST_START_TIME": START_ISO, "SPILLWAY_TEST_RNG_SEED": "1",
                "SPILLWAY_TEST_LOG_LEVEL": "INFO", "SPILLWAY_TEST_ADAPTER_PATHS": str(tmp_path / "none"),
                "SPILLWAY_TEST_RETENTION_POLL_SECONDS": "5", "SPILLWAY_TEST_RETENTION_STEPPED": "1",
                "SPILLWAY_TEST_NO_TOKEN": "1"})
    with _serve(env, tmp_path) as s:
        c = httpx.Client(base_url=s.url, timeout=60)
        assert c.post("/api/deployments", json=CLAUDE_MEMBER).status_code == 201
        assert c.post("/api/routes", json=TASK_ROUTE).status_code == 201
        r = c.post("/v1/chat/completions", json=TASK_BODY)
        assert r.status_code == 200, r.text
        assert r.headers["X-Spillway-Served-By"] == "claude-sub"
        c.close()
    names = json.loads((home / "cli_env_names.json").read_text(encoding="utf-8"))
    assert "SPILLWAY_HOME" in names   # 記錄確實來自 CLI 子行程
    assert not set(names) & set(TEST_CONTROL_ENV_NAMES), names


def test_l2_adapter_paths_conflict_is_startup_failure_exit_3(tmp_path):
    """`SPILLWAY_TEST_ADAPTER_PATHS` 載入與內建模組衝突的模組(7.6.6)→ lifespan 失敗 → 結束碼 3(行程契約)。"""
    home = tmp_path / "home"
    mods = tmp_path / "mods"
    write_module(mods, "dup_openai", providers=("openai",))
    env = _base_env(tmp_path)
    env.update({"SPILLWAY_HOME": str(home), "SPILLWAY_TEST_CONTROL": "1", "SPILLWAY_TEST_ADAPTER_PATHS": str(mods)})
    r = _run_until_exit(env)
    assert r.returncode == 3, r.stderr
    assert cli.TEST_CONTROL_WARNING in r.stderr.splitlines()

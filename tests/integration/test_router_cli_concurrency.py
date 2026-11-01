"""CHG-20261101-03 T6 L2:CLI 成員併發上限與 env 憑證遮罩(決策 7、9;NFR-6)。

`create_app()` + 假 `claude`(`fake_cli` fixture)+ 假 API 上游(`fake_api`)。claude-cli 成員經管理 API 建立。
"""
from __future__ import annotations

import asyncio
import dataclasses
import time

import pytest

from spillway.redact import REDACTOR
from spillway.store import repo
from spillway.store.repo import Route
from tests.helpers import api_member
from tests.integration.admin.helpers import KEY

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("fake_key_env")]

SECRET = "plainsecret-t6-l2-9f8e7d6c"   # 不符合預設遮罩樣式;遮罩必須來自成員 env 憑證值
TASK_BODY = {"model": "t", "messages": [{"role": "user", "content": "do it"}],
             "metadata": {"mode": "task"}}


async def _setup(h, fake_api, monkeypatch) -> None:
    r = await h.client.post("/api/deployments", json={
        "id": "claude-sub", "model_name": "claude-sub", "kind": "cli", "provider": "claude-cli",
        "model": "claude-sonnet-5-5", "params": {"max_concurrency": 1, "timeout": 60},
        "capabilities": {"ctx": 1000000, "tools": False, "vision": False,
                         "efforts": ["low", "medium", "high", "max"]},
        "tags": ["t"],
    })
    assert r.status_code == 201, r.text
    h.add_deployment(api_member("deepseek-api", fake_api.base_url, tags=("t",)))
    # 另一個(停用的)成員,憑證放在 claude 的固定清單不會移除的變數裡:決策 9 要求一併移除並遮罩
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    h.add_deployment(dataclasses.replace(
        api_member("other-api", fake_api.base_url, tags=("x",), enabled=False),
        credential_ref="env:OPENAI_API_KEY"))
    h.add_route(Route("t", {"tags": {"all": ["t"]}}, mode="task", record_context=True))


async def _wait_started(fake_cli, count: int = 1, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while len(fake_cli.records("claude")) < count:
        assert time.monotonic() < deadline, "假 claude 沒有啟動"
        await asyncio.sleep(0.02)


async def test_second_task_request_falls_to_api_member_while_cli_is_busy(
        spillway, fake_api, fake_cli, monkeypatch):
    h = spillway
    await _setup(h, fake_api, monkeypatch)
    fake_cli.set("claude", "hold_until_release")

    first = asyncio.ensure_future(h.client.post("/v1/chat/completions", json=TASK_BODY))
    try:
        await _wait_started(fake_cli)
        assert h.app.state.cli_runner.in_flight("claude-sub") == 1

        second = await h.client.post("/v1/chat/completions", json=TASK_BODY)
        assert second.status_code == 200, second.text
        assert second.headers["X-Spillway-Served-By"] == "deepseek-api"

        plan = await h.client.post("/api/routes/t/dry-run", json=TASK_BODY)
        assert plan.status_code == 200, plan.text
        excluded = {x["deployment"]: x["reason"] for x in plan.json()["excluded"]}
        assert excluded["claude-sub"] == "concurrency_full"
        assert [r.deployment_id for r in h.ledger()] == ["deepseek-api"]   # 被篩除不寫帳本
    finally:
        fake_cli.release("claude")
    resp = await asyncio.wait_for(first, 30)
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Spillway-Served-By"] == "claude-sub"
    assert h.app.state.cli_runner.in_flight("claude-sub") == 0

    rows = h.ledger()
    assert sorted(r.deployment_id for r in rows) == ["claude-sub", "deepseek-api"]
    assert [r.deployment_id for r in rows].count("claude-sub") == 1
    assert len(fake_cli.records("claude")) == 1


async def test_cli_output_with_member_env_credential_is_masked(
        spillway, fake_api, fake_cli, monkeypatch):
    h = spillway
    await _setup(h, fake_api, monkeypatch)
    assert not REDACTOR.redact(SECRET)[1]
    fake_cli.set("claude", "ok", result=f"echo {SECRET} and {KEY}")

    resp = await h.client.post("/v1/chat/completions", json=TASK_BODY)
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Spillway-Served-By"] == "claude-sub"
    (rec,) = fake_cli.wait_for_records("claude", 1, finished=True)
    assert rec["env_present"]["OPENAI_API_KEY"] is False      # 成員 env 憑證不進子行程

    (row,) = h.ledger()
    with h.db() as conn:
        p = repo.get_payload(conn, row.id)
    assert p is not None and p.redacted
    assert SECRET not in p.response_text and KEY not in p.response_text

    # 失敗路徑:錯誤訊息同樣遮罩(帳本 error 與 503 body)
    fake_cli.set("claude", "error", result=f"failed with {SECRET}")
    fake_api.set_mode("*", "http_500")
    resp = await h.client.post("/v1/chat/completions", json=TASK_BODY)
    assert resp.status_code == 503, resp.text
    assert SECRET not in resp.text
    rows = [r for r in h.ledger() if r.deployment_id == "claude-sub"]
    assert rows[0].status == "error" and SECRET not in (rows[0].error or "")

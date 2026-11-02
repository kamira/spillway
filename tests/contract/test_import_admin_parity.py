"""匯入與管理 API 規則一致(CHG-20261102-02 T4;決策 6、11;契約清單「匯入與管理 API 規則一致」)。

同一份會被管理 API 拒絕的資料(憑證鍵名、值樣式、`params.signals`、`credential_ref` 格式、群組名稱、
`single` 模組),經 `POST /api/config/import` 也被拒,狀態碼與錯誤碼相同;另以一份管理 API 接受的
資料確認匯入也接受(避免兩邊一起誤拒)。匯入失敗時資料庫不變。
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from spillway.app import create_app
from spillway.clock import FakeClock, SeqRng
from spillway.store import db as store_db

pytestmark = pytest.mark.anyio

YAML_CT = {"Content-Type": "application/yaml"}


@contextlib.asynccontextmanager
async def _client(db_path: Path, clock: FakeClock) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(clock=clock, rng=SeqRng([0.0]), db_path=db_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as c:
            yield c


def _member(dep_id: str, **kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"id": dep_id, "model_name": dep_id, "kind": "api",
                            "provider": "openai", "model": "gpt-4o-mini"}
    body.update(kw)
    return body


def _cli(dep_id: str) -> dict[str, Any]:
    return {"id": dep_id, "model_name": dep_id, "kind": "cli", "provider": "claude-cli",
            "model": "claude-sonnet-4-5"}


MEMBER_CASES = {
    "credential_key": _member("a", params={"api_key": "x"}),
    "credential_key_normalized": _member("a", params={"Access-Token": "x"}),
    "nested_credential_key": _member("a", params={"extra": {"client_secret": "x"}}),
    "secret_value": _member("a", params={"note": "sk-test-0000"}),
    "capabilities_secret": _member("a", capabilities={"password": "x"}),
    "signals_unknown_key": _member("a", params={"signals": {"bogus": []}}),
    "signals_not_mapping": _member("a", params={"signals": ["quota"]}),
    "credential_ref_format": _member("a", credential_ref="plain-value"),
    "bad_kind": _member("a", kind="bogus"),
    "id_with_space": _member("a b"),
    "id_trailing_newline": _member("a\n"),
    "budget_anchor": _member("a", budget={"type": "usd", "reset_anchor": 29}),
}

GROUP_CASES = [".", "..", "all", "a/b", "a b", "‮abc", "a b", "\x00x", "", "é",
               "a" * 201]


async def _snapshot(db_path: Path) -> list[str]:
    conn = store_db.connect(db_path)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


async def _import(c: httpx.AsyncClient, doc: dict[str, Any], mode: str = "merge") -> httpx.Response:
    text = yaml.safe_dump({"version": 1, **doc}, allow_unicode=True, sort_keys=False)
    return await c.post("/api/config/import", params={"mode": mode}, content=text.encode("utf-8"),
                        headers=YAML_CT)


def _code(r: httpx.Response) -> tuple[int, str | None]:
    body = r.json()
    return r.status_code, body.get("error", {}).get("code") if isinstance(body, dict) else None


@pytest.mark.parametrize("case", sorted(MEMBER_CASES))
async def test_member_rules_match(tmp_path, clock, case):
    body = MEMBER_CASES[case]
    async with _client(tmp_path / "a.db", clock) as c:
        admin = await c.post("/api/deployments", json=body)
    async with _client(tmp_path / "b.db", clock) as c:
        before = await _snapshot(tmp_path / "b.db")
        imported = await _import(c, {"deployments": [body]})
    assert _code(admin) == (422, "validation_error"), admin.text
    assert _code(imported) == _code(admin), imported.text
    assert await _snapshot(tmp_path / "b.db") == before


@pytest.mark.parametrize("name", GROUP_CASES)
async def test_group_name_rules_match(tmp_path, clock, name):
    async with _client(tmp_path / "a.db", clock) as c:
        admin = await c.post("/api/groups", json={"name": name, "selector": {}})
    async with _client(tmp_path / "b.db", clock) as c:
        imported = await _import(c, {"groups": {name: {}}})
    assert _code(admin) == (422, "validation_error"), admin.text
    assert _code(imported) == _code(admin), imported.text


ROUTE_NAME_CASES = ["a\n", "a b", "a/b", "a:b", "a" * 201, "\ta"]


@pytest.mark.parametrize("name", ROUTE_NAME_CASES)
async def test_route_name_rules_match(tmp_path, clock, name):
    target = {"tags": {"all": [], "any": [], "none": []}}
    async with _client(tmp_path / "a.db", clock) as c:
        admin = await c.post("/api/routes", json={"name": name, "target": target})
    async with _client(tmp_path / "b.db", clock) as c:
        before = await _snapshot(tmp_path / "b.db")
        imported = await _import(c, {"routes": {name: {"target": target}}})
    assert _code(admin) == (422, "validation_error"), admin.text
    assert _code(imported) == _code(admin), imported.text
    assert await _snapshot(tmp_path / "b.db") == before


async def test_single_rule_matches(tmp_path, clock):
    async with _client(tmp_path / "a.db", clock) as c:
        assert (await c.post("/api/deployments", json=_cli("c1"))).status_code == 201
        admin = await c.post("/api/deployments", json=_cli("c2"))
    async with _client(tmp_path / "b.db", clock) as c:
        assert (await _import(c, {"deployments": [_cli("c1")]})).status_code == 200
        imported = await _import(c, {"deployments": [_cli("c2")]})
        same_file = await _import(c, {"deployments": [_cli("c3"), _cli("c4")]}, "replace")
    assert _code(admin) == (409, "provider_single")
    assert _code(imported) == _code(admin)
    assert _code(same_file) == _code(admin)


@pytest.mark.parametrize("member,group", [
    (_member("ok-1", params={"max_tokens": 10, "cache_key": "k", "api_base": "http://127.0.0.1:1"},
             credential_ref="env:SPILLWAY_TEST_KEY"), "coding"),
    (_member("ok-2", params={"signals": {"quota": [{"equals": {"code": "x"}}]}}), "中文群組"),
    (_member("ok.3", tags=["a", "b"], budget={"type": "none", "window": "none"}), "a.b"),
])
async def test_accepted_by_both(tmp_path, clock, member, group):
    async with _client(tmp_path / "a.db", clock) as c:
        admin_dep = await c.post("/api/deployments", json=member)
        admin_group = await c.post("/api/groups", json={"name": group, "selector": {}})
    async with _client(tmp_path / "b.db", clock) as c:
        imported = await _import(c, {"deployments": [member], "groups": {group: {}}})
    assert admin_dep.status_code == 201, admin_dep.text
    assert admin_group.status_code == 201, admin_group.text
    assert imported.status_code == 200, imported.text

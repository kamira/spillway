"""`DELETE /api/deployments/{id}` 的連帶刪除(CHG-20261101-02 T10 L2;安全影響 irreversible-delete:行為不變,只補測)。

經 `create_app()`(`tests/integration/conftest.py` 的 `open_spillway`)打真實 app:
刪除後 deployment_tags、budgets、route_targets 無該 id;usage_ledger 與 usage_payloads 列保留;
`/api/dashboard` 不列該 id;`/api/ledger` 仍可查到孤兒列;只含該成員的 route 回 503 `no_candidate`。
"""
from __future__ import annotations

import pytest

from spillway.domain import Budget
from spillway.store import repo
from tests.integration.conftest import api_member

from .helpers import assert_error

pytestmark = pytest.mark.anyio

MSGS = [{"role": "user", "content": "hello"}]


async def test_delete_cascades_config_and_keeps_ledger(open_spillway, fake_api):
    async with open_spillway() as h:
        h.add_deployment(api_member("gone", fake_api.base_url, tags=("solo", "x")))
        h.add_deployment(api_member("stay", fake_api.base_url, model="gpt-4.1-mini", tags=("other",)))
        with h.db() as conn:
            repo.save_budget(conn, "gone", Budget(type="usd", limit_amount=10.0))
        h.add_route(repo.Route(name="solo", target={"tags": {"all": ["solo"]}}, record_context=True),
                    [repo.RouteTarget("gone", 1, 0)])

        h.add_route(repo.Route(name="other", target={"tags": {"all": ["other"]}}))

        r = await h.client.post("/v1/chat/completions", json={"model": "solo", "messages": MSGS})
        assert r.status_code == 200, r.text
        # 另一個成員的帳本列:讓下方 `?deployment=gone` 篩選確實排除一列
        r_other = await h.client.post("/v1/chat/completions", json={"model": "other", "messages": MSGS})
        assert r_other.status_code == 200, r_other.text
        assert r_other.headers["X-Spillway-Served-By"] == "stay"
        (row,) = h.ledger(deployment_id="gone")
        assert len(h.ledger()) == 2

        with h.db() as conn:
            def count(table: str) -> int:
                return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE deployment_id = ?",
                                    ("gone",)).fetchone()[0]
            assert count("deployment_tags") == 2
            assert count("budgets") == 1
            assert count("route_targets") == 1

        d = await h.client.delete("/api/deployments/gone")
        assert d.status_code == 204, d.text

        with h.db() as conn:
            for table in ("deployments", "deployment_tags", "budgets", "route_targets"):
                col = "id" if table == "deployments" else "deployment_id"
                n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", ("gone",)).fetchone()[0]
                assert n == 0, table
            assert conn.execute("SELECT COUNT(*) FROM usage_ledger WHERE deployment_id = 'gone'").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM usage_payloads WHERE ledger_id = ?",
                                (row.id,)).fetchone()[0] == 1
            # 未涉及的成員不受影響
            assert repo.get_tags(conn, "stay") == ("other",)

        dash = (await h.client.get("/api/dashboard")).json()
        ids = [x["id"] for x in dash["deployments"]]
        assert "gone" not in ids and "stay" in ids

        led = await h.client.get("/api/ledger", params={"deployment": "gone"})
        assert led.status_code == 200, led.text
        items = led.json()["items"]
        assert [i["id"] for i in items] == [row.id]
        assert items[0]["deployment_id"] == "gone"

        r2 = await h.client.post("/v1/chat/completions", json={"model": "solo", "messages": MSGS})
        assert_error(r2, 503, "no_candidate")

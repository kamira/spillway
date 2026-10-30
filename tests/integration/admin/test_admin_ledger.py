"""`GET /api/ledger`(篩選、分頁)與 `GET /api/ledger/{id}/payload`(決策 17)(CHG-20261030-02 T14)。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from spillway.ledger import Attempt, LedgerWriter
from spillway.store import repo

from .helpers import KEY, assert_error

pytestmark = pytest.mark.anyio


def add(db, clock, *, minutes_ago=0, status="ok", dep="a", route="rt", **kw) -> int:
    return repo.insert_ledger(db, repo.LedgerRow(
        ts=clock.now() - timedelta(minutes=minutes_ago), status=status, deployment_id=dep,
        route_name=route, **kw))


async def test_ledger_pagination_newest_first(client, db, clock):
    ids = [add(db, clock, minutes_ago=i) for i in range(120)]     # ids[0] 最新
    r = await client.get("/api/ledger")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["page"] == 1 and out["page_size"] == 50 and out["total"] == 120
    assert out["pages"] == 3
    assert [x["id"] for x in out["items"]] == ids[:50]
    p3 = (await client.get("/api/ledger", params={"page": 3})).json()
    assert [x["id"] for x in p3["items"]] == ids[100:]
    p9 = (await client.get("/api/ledger", params={"page": 9})).json()
    assert p9["items"] == [] and p9["total"] == 120
    small = (await client.get("/api/ledger", params={"page": 2, "page_size": 7})).json()
    assert [x["id"] for x in small["items"]] == ids[7:14] and small["pages"] == 18


async def test_ledger_item_fields(client, db, clock):
    lid = add(db, clock, tokens_in=3, tokens_out=4, cost_usd=0.5, slot=2, fallback_depth=1,
              latency_ms=9, effort="high", est_tokens_in=3, est_tokens_out_p50=500,
              est_tokens_out_p90=2000, est_cost_usd_p90=0.1, error=None)
    (item,) = (await client.get("/api/ledger")).json()["items"]
    assert item == {"id": lid, "ts": "2026-10-30T12:00:00Z", "route_name": "rt",
                    "deployment_id": "a", "effort": "high", "tokens_in": 3, "tokens_out": 4,
                    "cost_usd": 0.5, "status": "ok", "slot": 2, "fallback_depth": 1,
                    "latency_ms": 9, "est_tokens_in": 3, "est_tokens_out_p50": 500,
                    "est_tokens_out_p90": 2000, "est_cost_usd_p90": 0.1, "error": None}


async def test_ledger_filters(client, db, clock):
    a_old = add(db, clock, minutes_ago=600, dep="a", route="rt")
    a_err = add(db, clock, minutes_ago=30, dep="a", route="rt", status="error")
    b_ok = add(db, clock, minutes_ago=20, dep="b", route="other")
    direct = add(db, clock, minutes_ago=10, dep="q", route=None)

    async def ids(**params):
        r = await client.get("/api/ledger", params=params)
        assert r.status_code == 200, r.text
        return [x["id"] for x in r.json()["items"]]

    assert await ids(deployment="a") == [a_err, a_old]
    assert await ids(route="other") == [b_ok]
    assert await ids(status="error") == [a_err]
    assert await ids(**{"from": "2026-10-30T11:00:00Z"}) == [direct, b_ok, a_err]
    assert await ids(to="2026-10-30T11:40:00Z") == [a_err, a_old]     # to 不含
    assert await ids(**{"from": "2026-10-30", "to": "2026-10-30T11:45:00+00:00"}) == \
        [b_ok, a_err, a_old]


@pytest.mark.parametrize("params,path", [
    ({"page": 0}, "query.page"),
    ({"page": "x"}, "query.page"),
    ({"page": 1_000_001}, "query.page"),                  # S2:上限,超大值不得 500
    ({"page": 10**19}, "query.page"),
    ({"page_size": 1000}, "query.page_size"),
    ({"status": "weird"}, "query.status"),
    ({"from": "yesterday"}, "query.from"),
])
async def test_ledger_query_validation(client, params, path):
    msg = assert_error(await client.get("/api/ledger", params=params), 422, "validation_error")
    assert path in msg


async def test_ledger_page_max_is_accepted(client):
    out = (await client.get("/api/ledger", params={"page": 1_000_000, "page_size": 500})).json()
    assert out["page"] == 1_000_000 and out["items"] == []


def record(db, clock, *, ts=None, route_record_context=None, text="hello") -> int:
    return LedgerWriter(db).record(Attempt(
        ts=ts or clock.now(), status="ok", route_name="rt", deployment_id="a",
        request_text=f'[{{"role": "user", "content": "{text}"}}]', response_text="hi there",
        extra_secrets=(), route_record_context=route_record_context))


async def test_payload_ok(client, db, clock):
    lid = record(db, clock, text=f"my key {KEY}")
    r = await client.get(f"/api/ledger/{lid}/payload")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ledger_id"] == lid and out["ts"] == "2026-10-30T12:00:00Z"
    assert out["response_text"] == "hi there"
    assert out["redacted"] is True and out["truncated"] is False
    assert KEY not in r.text and "my key" in out["request_json"]
    assert out["bytes"] > 0


async def test_payload_expired_410(client, db, clock):
    lid = record(db, clock)
    clock.advance(days=8)                       # 預設保存 7 天,清理尚未執行也判過期
    assert_error(await client.get(f"/api/ledger/{lid}/payload"), 410, "payload_expired")


async def test_payload_never_recorded_and_missing_404(client, db, clock):
    lid = record(db, clock, route_record_context=False)
    assert_error(await client.get(f"/api/ledger/{lid}/payload"), 404, "not_found")
    assert_error(await client.get("/api/ledger/99999/payload"), 404, "not_found")
    assert "path.ledger_id" in assert_error(await client.get("/api/ledger/abc/payload"), 422,
                                            "validation_error")

"""`GET /api/ledger/export.csv`(CHG-20261102-02 T5;決策 9、19;決策審議 AM-10)。

L1:篩選列集合同 `/api/ledger`、標題列逐字、BOM、標頭、公式注入防護、假金鑰、100,000 上限、
`repo.iter_ledger` 以 `fetchmany` 分批。
L2:連線開關數相等且都在事件迴圈執行緒(延伸 R23,含用戶端中途斷線);記憶體與時間記錄。
"""
from __future__ import annotations

import asyncio
import csv
import io
import threading
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from spillway.api.admin import ledger as ledger_api
from spillway.clock import to_iso
from spillway.ledger import Attempt, LedgerWriter
from spillway.store import repo

from tests.integration.test_request_conn import _Counting

from .helpers import KEY, assert_error

pytestmark = pytest.mark.anyio

HEADER = ("id,ts,route_name,deployment_id,effort,tokens_in,tokens_out,cost_usd,status,slot,"
          "fallback_depth,latency_ms,est_tokens_in,est_tokens_out_p50,est_tokens_out_p90,"
          "est_cost_usd_p90,error")


def add(db, clock, *, minutes_ago=0, status="ok", dep="a", route="rt", **kw) -> int:
    return repo.insert_ledger(db, repo.LedgerRow(
        ts=clock.now() - timedelta(minutes=minutes_ago), status=status, deployment_id=dep,
        route_name=route, **kw))


def bulk(db, clock, n: int, *, error: str | None = None) -> None:
    """以 SQL 直接種入 n 列(大量資料用;`ts` 每列早一秒)。"""
    base = clock.now()
    db.execute("BEGIN")
    db.executemany(
        "INSERT INTO usage_ledger (ts, route_name, deployment_id, status, tokens_in, tokens_out, "
        "cost_usd, fallback_depth, error) VALUES (?, 'rt', 'member-a', 'ok', 120, 340, 0.00125, 0, ?)",
        ((to_iso(base - timedelta(seconds=i)), error) for i in range(n)))
    db.execute("COMMIT")


def parse(text: str) -> list[list[str]]:
    assert text.startswith("﻿")
    return list(csv.reader(io.StringIO(text[1:])))


# ---------------------------------------------------------------- L1


async def test_header_bom_and_response_headers(client, clock):
    r = await client.get("/api/ledger/export.csv")
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"\xef\xbb\xbf")
    assert r.text.split("\r\n")[0] == "﻿" + HEADER
    assert parse(r.text) == [HEADER.split(",")]
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"] == \
        'attachment; filename="spillway-ledger-20261030T120000Z.csv"'


async def test_filename_uses_clock_in_utc(make_app, make_client, clock):
    app = make_app()
    app.state.clock = SimpleNamespace(
        now=lambda: datetime(2026, 11, 2, 9, 8, 7, tzinfo=timezone(timedelta(hours=8))))
    async with make_client(app) as c:
        r = await c.get("/api/ledger/export.csv")
    assert r.headers["content-disposition"] == \
        'attachment; filename="spillway-ledger-20261102T010807Z.csv"'


async def test_row_values_same_as_ledger_items(client, db, clock):
    add(db, clock, tokens_in=3, tokens_out=4, cost_usd=0.5, slot=2, fallback_depth=1,
        latency_ms=9, effort="high", est_tokens_in=3, est_tokens_out_p50=500,
        est_tokens_out_p90=2000, est_cost_usd_p90=0.1, error=None)
    add(db, clock, minutes_ago=5, status="error", route=None, error="boom")
    items = (await client.get("/api/ledger")).json()["items"]
    rows = parse((await client.get("/api/ledger/export.csv")).text)
    cols = rows[0]
    assert len(rows) == 3
    for item, row in zip(items, rows[1:]):
        assert row == ["" if item[c] is None else str(item[c]) for c in cols]
    assert rows[1][1] == "2026-10-30T12:00:00Z"
    assert rows[2][cols.index("route_name")] == "" and rows[2][cols.index("slot")] == ""


@pytest.mark.parametrize("params", [
    {}, {"deployment": "a"}, {"route": "rt"}, {"status": "error"},
    {"from": "2026-10-30T11:45:00Z"}, {"to": "2026-10-30T11:45:00Z"},
    {"from": "2026-10-30", "to": "2026-10-30T11:55:00+00:00", "deployment": "a"},
])
async def test_filters_match_ledger_list(client, db, clock, params):
    add(db, clock, minutes_ago=600, dep="a", route="rt")
    add(db, clock, minutes_ago=30, dep="a", route="rt", status="error")
    add(db, clock, minutes_ago=20, dep="b", route="other")
    add(db, clock, minutes_ago=10, dep="q", route=None)
    add(db, clock, minutes_ago=1, dep="a", route="rt")
    listed = (await client.get("/api/ledger", params={**params, "page_size": 500})).json()
    r = await client.get("/api/ledger/export.csv", params=params)
    assert r.status_code == 200, r.text
    assert [int(row[0]) for row in parse(r.text)[1:]] == [x["id"] for x in listed["items"]]


@pytest.mark.parametrize("params,path", [
    ({"from": "yesterday"}, "query.from"),
    ({"to": "2026-13-01"}, "query.to"),
    ({"status": "nope"}, "query.status"),
])
async def test_invalid_query_422(client, params, path):
    msg = assert_error(await client.get("/api/ledger/export.csv", params=params), 422,
                       "validation_error")
    assert path in msg


@pytest.mark.parametrize("value", ["=1+1", "@x", "-2", "+3", "\tTAB", "\rCR"])
async def test_formula_injection_guard_on_str_columns(client, db, clock, value):
    add(db, clock, dep=value, route=value, error=value, effort=value)
    rows = parse((await client.get("/api/ledger/export.csv")).text)
    cols, row = rows[0], rows[1]
    for c in ("deployment_id", "route_name", "error", "effort"):
        assert row[cols.index(c)] == "'" + value, c


async def test_numeric_columns_not_prefixed(client, db, clock):
    add(db, clock, tokens_in=-1, tokens_out=-2, cost_usd=-0.5, latency_ms=-3, slot=-4,
        est_cost_usd_p90=-0.25)
    rows = parse((await client.get("/api/ledger/export.csv")).text)
    cols, row = rows[0], rows[1]
    assert [row[cols.index(c)] for c in ("tokens_in", "tokens_out", "cost_usd", "latency_ms",
                                         "slot", "est_cost_usd_p90")] == \
        ["-1", "-2", "-0.5", "-3", "-4", "-0.25"]
    assert not row[0].startswith("'")


async def test_hyperlink_deployment_seeded_by_sql_is_prefixed(client, db, clock):
    db.execute(
        "INSERT INTO deployments (id, model_name, kind, provider, model) "
        "VALUES ('=HYPERLINK(x)', 'm', 'local', 'hosted_vllm', 'local-model')")
    add(db, clock, dep="=HYPERLINK(x)")
    r = await client.get("/api/ledger/export.csv")
    row = parse(r.text)[1]
    assert row[3] == "'=HYPERLINK(x)"
    assert ",=HYPERLINK" not in r.text


async def test_error_column_has_no_fake_key(client, db, clock):
    LedgerWriter(db).record(Attempt(ts=clock.now(), status="error", deployment_id="a",
                                    route_name="rt", error=f"401 bad key {KEY}",
                                    extra_secrets=(KEY,)))
    r = await client.get("/api/ledger/export.csv")
    assert r.status_code == 200
    assert r.text.count(KEY) == 0
    assert "401 bad key" in r.text


async def test_over_100k_rows_is_422(client, db, clock):
    bulk(db, clock, ledger_api.LEDGER_CSV_MAX_ROWS + 1)
    msg = assert_error(await client.get("/api/ledger/export.csv"), 422, "validation_error")
    assert "100000" in msg
    # 篩選縮小後可匯出
    r = await client.get("/api/ledger/export.csv", params={"from": "2026-10-30T11:59:00Z"})
    assert r.status_code == 200 and len(parse(r.text)) == 1 + 61


async def test_exactly_100k_rows_ok(client, db, clock):
    bulk(db, clock, ledger_api.LEDGER_CSV_MAX_ROWS)
    r = await client.get("/api/ledger/export.csv")
    assert r.status_code == 200
    assert len(parse(r.text)) == 1 + ledger_api.LEDGER_CSV_MAX_ROWS


class _CursorSpy:
    def __init__(self, cur, calls):
        self._cur, self._calls = cur, calls

    def fetchmany(self, n):
        self._calls.append(n)
        return self._cur.fetchmany(n)

    def __getattr__(self, name):
        if name in ("fetchall", "fetchone", "__iter__"):
            raise AssertionError(f"iter_ledger 不可使用 {name}")
        return getattr(self._cur, name)


class _ConnSpy:
    def __init__(self, conn):
        self._conn, self.calls, self.sql = conn, [], []

    def execute(self, sql, *args):
        self.sql.append(sql)
        return _CursorSpy(self._conn.execute(sql, *args), self.calls)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_iter_ledger_uses_fetchmany_batches_and_read_transaction(db, clock):
    bulk(db, clock, 25)
    spy = _ConnSpy(db)
    batches = list(repo.iter_ledger(spy, limit=23, batch=10))
    assert [len(b) for b in batches] == [10, 10, 3]
    assert spy.calls and set(spy.calls) == {10}
    assert spy.sql[0] == "BEGIN" and spy.sql[-1] == "COMMIT"
    assert not db.in_transaction
    flat = [r.id for b in batches for r in b]
    assert flat == [r.id for r in repo.list_ledger(db, limit=23)]


def test_iter_ledger_filters_and_rollback_on_early_close(db, clock):
    add(db, clock, dep="a")
    add(db, clock, minutes_ago=1, dep="b")
    add(db, clock, minutes_ago=2, dep="a", status="error")
    got = [r.id for b in repo.iter_ledger(db, deployment_id="a", status="ok", limit=10) for r in b]
    assert got == [r.id for r in repo.list_ledger(db, deployment_id="a", status="ok")]
    it = repo.iter_ledger(db, limit=10, batch=1)
    next(it)
    assert db.in_transaction
    it.close()
    assert not db.in_transaction


# ---------------------------------------------------------------- L2


def _alive(conn) -> bool:
    try:
        conn.execute("SELECT 1")
    except Exception:
        return False
    return True


async def test_connections_opened_and_closed_on_loop_thread(client, db, clock):
    bulk(db, clock, 2500)
    app = client._transport.app
    f = _Counting(app.state.conn_factory)
    app.state.conn_factory = f
    for _ in range(3):
        r = await client.get("/api/ledger/export.csv")
        assert r.status_code == 200 and len(parse(r.text)) == 2501
    assert len(f.opened) == 6                     # 每次:預檢一條 + generator 一條
    assert f.closed_count() == 6
    assert set(f.threads) == {threading.get_ident()}


async def test_connection_closed_when_client_disconnects_mid_stream(client, db, clock):
    bulk(db, clock, 5000)
    app = client._transport.app
    f = _Counting(app.state.conn_factory)
    app.state.conn_factory = f
    first_body = asyncio.Event()
    sent: list[dict] = []
    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": b"", "more_body": False}
        await first_body.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            first_body.set()
            await asyncio.sleep(0)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": "/api/ledger/export.csv", "raw_path": b"/api/ledger/export.csv",
             "query_string": b"", "root_path": "",
             "headers": [(b"host", b"127.0.0.1:8787")], "client": ("127.0.0.1", 50000),
             "server": ("127.0.0.1", 8787)}
    await asyncio.wait_for(app(scope, receive, send), timeout=10)
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 200
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert 0 < body.count(b"\r\n") < 5001         # 中途停止,未送完
    assert not any(m.get("more_body") is False for m in sent[1:])
    assert len(f.opened) == 2 and f.closed_count() == 2
    assert set(f.threads) == {threading.get_ident()}


async def _drain(app, **params) -> int:
    req = SimpleNamespace(app=app)
    resp = await ledger_api.export_ledger_csv(req, **params)
    n = 0
    async for chunk in resp.body_iterator:
        n += len(chunk)
    return n


async def test_memory_and_time_recorded(make_app, db, clock, record_property):
    """tracemalloc 峰值:50,000 列與 10,000 列相差 < 5 MB;10,000 列匯出時間記錄(非閘門)。"""
    app = make_app()
    bulk(db, clock, 10_000)

    tracemalloc.start()
    t0 = time.perf_counter()
    await _drain(app)
    elapsed_10k = time.perf_counter() - t0
    peak_10k = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    bulk_more = 40_000
    base = clock.now() - timedelta(days=1)
    db.execute("BEGIN")
    db.executemany(
        "INSERT INTO usage_ledger (ts, route_name, deployment_id, status, tokens_in, tokens_out, "
        "cost_usd, fallback_depth) VALUES (?, 'rt', 'member-a', 'ok', 120, 340, 0.00125, 0)",
        ((to_iso(base - timedelta(seconds=i)),) for i in range(bulk_more)))
    db.execute("COMMIT")

    tracemalloc.start()
    await _drain(app)
    peak_50k = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    record_property("csv_10k_seconds", round(elapsed_10k, 3))
    record_property("csv_peak_10k_bytes", peak_10k)
    record_property("csv_peak_50k_bytes", peak_50k)
    print(f"\n[T5 L2] 10k rows {elapsed_10k:.3f}s; tracemalloc peak 10k={peak_10k} B, "
          f"50k={peak_50k} B, diff={peak_50k - peak_10k} B")
    assert peak_50k - peak_10k < 5 * 1024 * 1024


# ---------------------------------------------------------------- min_depth
# CHG-20261103-01 T2(決策 10 ④;AM-13)


@pytest.mark.parametrize("params", [{"min_depth": 1}, {"min_depth": 2, "deployment": "a"},
                                    {"min_depth": 0}])
async def test_csv_min_depth_matches_ledger_list(client, db, clock, params):
    for i, depth in enumerate([0, 1, 2, 0, 1, 3]):
        add(db, clock, minutes_ago=i, dep="a" if i % 2 else "b", fallback_depth=depth)
    listed = (await client.get("/api/ledger", params={**params, "page_size": 500})).json()
    r = await client.get("/api/ledger/export.csv", params=params)
    assert r.status_code == 200, r.text
    rows = parse(r.text)
    depth_col = rows[0].index("fallback_depth")
    assert [int(row[0]) for row in rows[1:]] == [x["id"] for x in listed["items"]]
    assert rows[1:] and all(int(row[depth_col]) >= params["min_depth"] for row in rows[1:])


@pytest.mark.parametrize("value", ["-1", "101", "x"])
async def test_csv_min_depth_invalid(client, value):
    msg = assert_error(await client.get("/api/ledger/export.csv", params={"min_depth": value}), 422,
                       "validation_error")
    assert "query.min_depth" in msg

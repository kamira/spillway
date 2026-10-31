"""T9 estimate 單元測試(spec 5.4.1–5.4.4;CHG-20261030-02 T9 L1、L2)。不連網。"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone

import litellm
import pytest

from spillway.domain import ChatRequest, Deployment
from spillway.estimate import Estimate, Estimator, percentile_nearest_rank
from spillway.store.repo import LedgerRow, insert_ledger

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
MSGS = [{"role": "user", "content": "請把這段文字翻成英文:今天天氣很好。"}]

PRICED = Deployment(
    id="d-sonnet", model_name="sonnet", kind="api", provider="anthropic", model="claude-sonnet-4-5"
)
UNPRICED = Deployment(
    id="d-bogus", model_name="bogus", kind="api", provider="nosuchprovider", model="no-such-model-x"
)
LOCAL = Deployment(id="d-local", model_name="llama", kind="local", provider="ollama", model="llama3")
CODEX = Deployment(id="d-codex", model_name="codex", kind="cli", provider="codex-cli", model="gpt-5")


def req(max_tokens: int | None = None, messages: list | None = None) -> ChatRequest:
    return ChatRequest(model="r", messages=messages or MSGS, max_tokens=max_tokens)


def add(db: sqlite3.Connection, i: int, status: str, tokens_out: int | None,
        dep_id: str = PRICED.id, route: str | None = "r1") -> None:
    insert_ledger(db, LedgerRow(ts=T0 + timedelta(minutes=i), status=status, route_name=route,
                                deployment_id=dep_id, tokens_out=tokens_out))


def reference_percentile(values: list[int], p: float) -> int:
    """依 docstring 的 nearest-rank 定義另行計算:排序後第 ceil(p/100*n) 個(1 起算)。"""
    s = sorted(values)
    return s[math.ceil(p * len(s) / 100) - 1]


# ---------------------------------------------------------------- L1

def test_max_tokens_sets_p50_and_p90(db):
    for i in range(20):
        add(db, i, "ok", 10_000 + i)
    est = Estimator(db).estimate(PRICED, req(max_tokens=300), "r1")
    assert (est.out_p50, est.out_p90) == (300, 300)


def test_fewer_than_five_ok_rows_uses_defaults(db):
    for i in range(4):
        add(db, i, "ok", 42)
    est = Estimator(db).estimate(PRICED, req(), "r1")
    assert (est.out_p50, est.out_p90) == (500, 2000)


def test_unpriced_model_cost_is_none(db):
    est = Estimator(db).estimate(UNPRICED, req(), "r1")
    assert est.cost_usd_p90 is None
    assert est.tokens_in > 0


def test_local_and_codex_cli_cost_is_none(db):
    assert Estimator(db).estimate(LOCAL, req(), None).cost_usd_p90 is None
    assert Estimator(db).estimate(CODEX, req(), "r1").cost_usd_p90 is None


def test_priced_model_cost_uses_local_price_table(db):
    est = Estimator(db).estimate(PRICED, req(max_tokens=300), "r1")
    price = litellm.model_cost["claude-sonnet-4-5"]
    expected = est.tokens_in * price["input_cost_per_token"] + 300 * price["output_cost_per_token"]
    assert est.cost_usd_p90 == pytest.approx(expected)


def test_tokens_in_uses_litellm_token_counter(db):
    expected = litellm.token_counter(model="anthropic/claude-sonnet-4-5", messages=MSGS)
    assert Estimator(db).estimate(PRICED, req(), "r1").tokens_in == expected


def test_tokens_in_falls_back_to_chars_div_4(db, monkeypatch):
    def boom(**_kw):
        raise RuntimeError("tokenizer unavailable")

    monkeypatch.setattr(litellm, "token_counter", boom)
    msgs = [{"role": "user", "content": "x" * 41}]
    assert Estimator(db).estimate(PRICED, req(messages=msgs), "r1").tokens_in == 11


def test_cli_overhead_added(db):
    base = Deployment(id="d-cli", model_name="c", kind="cli", provider="claude-cli",
                      model="claude-sonnet-4-5")
    with_oh = Deployment(**{**base.__dict__, "params": {"cli_overhead_tokens": 1234}})
    e = Estimator(db)
    assert e.estimate(with_oh, req(), "r1").tokens_in == e.estimate(base, req(), "r1").tokens_in + 1234


def test_estimate_is_frozen(db):
    est = Estimator(db).estimate(PRICED, req(), "r1")
    assert isinstance(est, Estimate)
    with pytest.raises(AttributeError):
        est.out_p50 = 1  # type: ignore[misc]


# ---------------------------------------------------------------- L2

def test_only_latest_50_ok_rows_are_used(db):
    # 60 筆 ok(tokens_out 為打散的不重複值)、10 筆 error 與 5 筆 skipped_precheck 穿插其中,
    # 非 ok 列給極大值,若被誤算會明顯改變百分位。另加其他 route、其他成員的干擾列。
    ok_rows: list[tuple[int, int]] = []
    minute = 0
    for k in range(60):
        val = (k * 37) % 61 * 10 + 5
        add(db, minute, "ok", val)
        ok_rows.append((minute, val))
        minute += 1
        if k % 6 == 0 and k // 6 < 10:
            add(db, minute, "error", 999_999)
            minute += 1
        if k % 12 == 5:
            add(db, minute, "skipped_precheck", 888_888)
            minute += 1
    add(db, minute + 1, "ok", 777_777, route="other")
    add(db, minute + 2, "ok", 666_666, dep_id="d-other")
    add(db, minute + 3, "ok", 555_555, route=None)

    n_err = db.execute("SELECT COUNT(*) FROM usage_ledger WHERE status='error'").fetchone()[0]
    n_skip = db.execute("SELECT COUNT(*) FROM usage_ledger WHERE status='skipped_precheck'").fetchone()[0]
    assert (n_err, n_skip) == (10, 5)

    latest50 = [v for _, v in sorted(ok_rows, reverse=True)[:50]]
    est = Estimator(db).estimate(PRICED, req(), "r1")
    assert est.out_p50 == reference_percentile(latest50, 50)
    assert est.out_p90 == reference_percentile(latest50, 90)
    # 若改取全部 60 筆 ok,結果不同——證明窗口確實是 50 筆
    all60 = [v for _, v in ok_rows]
    assert (reference_percentile(all60, 50), reference_percentile(all60, 90)) != (
        est.out_p50, est.out_p90)


def test_null_route_matches_only_direct_calls(db):
    for i in range(5):
        add(db, i, "ok", 100 * (i + 1), route=None)
        add(db, 10 + i, "ok", 9_000, route="r1")
    est = Estimator(db).estimate(PRICED, req(), None)
    assert (est.out_p50, est.out_p90) == (300, 500)


def test_null_tokens_out_rows_are_skipped(db):
    for i in range(4):
        add(db, i, "ok", 10 * (i + 1))
    add(db, 10, "ok", None)
    e = Estimator(db)
    assert (e.estimate(PRICED, req(), "r1").out_p50,) == (500,)
    add(db, 11, "ok", 50)
    assert (e.estimate(PRICED, req(), "r1").out_p50, e.estimate(PRICED, req(), "r1").out_p90) == (30, 50)


def test_conn_factory_accepted(db):
    for i in range(5):
        add(db, i, "ok", 7)
    est = Estimator(lambda: db).estimate(PRICED, req(), "r1")
    assert (est.out_p50, est.out_p90) == (7, 7)


@pytest.mark.parametrize(
    ("values", "p", "expected"),
    [([1, 2, 3, 4, 5], 50, 3), ([1, 2, 3, 4, 5], 90, 5), (list(range(1, 51)), 50, 25),
     (list(range(1, 51)), 90, 45), ([5, 1, 4, 2, 3, 6], 50, 3)],
)
def test_percentile_nearest_rank(values, p, expected):
    assert percentile_nearest_rank(values, p) == expected

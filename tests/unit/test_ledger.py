"""T10:LedgerWriter、RetentionJob、payload_status(spec 2.1.5、2.4、4.5.1;決策 17、19)。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from spillway.clock import FakeClock
from spillway.ledger import (
    LEDGER_STATUSES,
    Attempt,
    LedgerNotFound,
    LedgerWriter,
    RetentionJob,
    payload_status,
)
from spillway.redact import MASK
from spillway.store import repo

KEY = "sk-test-0000"
T0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
CONTENT_COLS = {"request_json", "response_text", "request_text", "prompt", "messages", "response"}


def _att(**kw) -> Attempt:
    base = dict(
        ts=T0,
        status="ok",
        route_name="r1",
        deployment_id="d1",
        request_text=json.dumps([{"role": "user", "content": "hello"}]),
        response_text="hi there",
    )
    base.update(kw)
    return Attempt(**base)


def _payload_row(db, lid):
    return db.execute("SELECT * FROM usage_payloads WHERE ledger_id = ?", (lid,)).fetchone()


# ---------------------------------------------------------------- L1


def test_ledger_row_has_no_content_columns_and_fields_written(db):
    w = LedgerWriter(db)
    att = _att(
        effort="high",
        tokens_in=10,
        tokens_out=20,
        cost_usd=0.5,
        slot=2,
        fallback_depth=1,
        latency_ms=123,
        est_tokens_in=11,
        est_tokens_out_p50=15,
        est_tokens_out_p90=30,
        est_cost_usd_p90=0.9,
    )
    lid = w.record(att)
    row = db.execute("SELECT * FROM usage_ledger WHERE id = ?", (lid,)).fetchone()
    assert not (set(row.keys()) & CONTENT_COLS)
    # 任何欄位都不含請求或回應內容(2.1.5)
    for k in row.keys():
        assert "hello" not in str(row[k]) and "hi there" not in str(row[k])
    got = repo.get_ledger(db, lid)
    assert (got.ts, got.status, got.route_name, got.deployment_id, got.effort) == (T0, "ok", "r1", "d1", "high")
    assert (got.tokens_in, got.tokens_out, got.cost_usd, got.slot, got.fallback_depth, got.latency_ms) == (
        10, 20, 0.5, 2, 1, 123,
    )
    assert (got.est_tokens_in, got.est_tokens_out_p50, got.est_tokens_out_p90, got.est_cost_usd_p90) == (
        11, 15, 30, 0.9,
    )
    p = repo.get_payload(db, lid)
    assert p.request_json == att.request_text and p.response_text == "hi there"
    assert p.ts == T0
    assert p.bytes == len(att.request_text.encode()) + len("hi there".encode())
    assert (p.truncated, p.redacted) == (False, False)


@pytest.mark.parametrize("status", sorted(LEDGER_STATUSES))
def test_all_statuses_accepted(db, status):
    lid = LedgerWriter(db).record(_att(status=status))
    assert repo.get_ledger(db, lid).status == status


def test_unknown_status_rejected_and_nothing_written(db):
    with pytest.raises(ValueError):
        LedgerWriter(db).record(_att(status="pending"))
    assert db.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0] == 0


def test_payload_over_limit_truncated_to_exact_limit(db):
    repo.set_setting(db, "context_max_bytes", 100)
    lid = LedgerWriter(db).record(_att(request_text="a" * 80, response_text="b" * 80))
    p = repo.get_payload(db, lid)
    assert p.truncated is True
    assert p.bytes == 100
    assert len(p.request_json.encode()) + len(p.response_text.encode()) == 100
    assert p.request_json == "a" * 50 and p.response_text == "b" * 50


def test_short_side_kept_whole_long_side_gets_rest(db):
    repo.set_setting(db, "context_max_bytes", 100)
    lid = LedgerWriter(db).record(_att(request_text="a" * 10, response_text="b" * 500))
    p = repo.get_payload(db, lid)
    assert p.request_json == "a" * 10 and p.response_text == "b" * 90 and p.bytes == 100 and p.truncated


def test_payload_at_limit_not_truncated(db):
    repo.set_setting(db, "context_max_bytes", 100)
    lid = LedgerWriter(db).record(_att(request_text="a" * 60, response_text="b" * 40))
    p = repo.get_payload(db, lid)
    assert (p.bytes, p.truncated) == (100, False)


def test_response_none_payload(db):
    repo.set_setting(db, "context_max_bytes", 10)
    lid = LedgerWriter(db).record(_att(status="error", request_text="x" * 30, response_text=None))
    p = repo.get_payload(db, lid)
    assert p.response_text is None and p.request_json == "x" * 10 and p.bytes == 10 and p.truncated


def test_truncation_does_not_split_multibyte_chars(db):
    repo.set_setting(db, "context_max_bytes", 10)
    # 「中」為 3 bytes;請求取 5 bytes → 只能放 1 個字
    lid = LedgerWriter(db).record(_att(request_text="中文字串", response_text="回應內容"))
    p = repo.get_payload(db, lid)
    assert p.truncated is True
    assert p.request_json == "中" and p.response_text == "回"
    assert p.bytes == len(p.request_json.encode()) + len(p.response_text.encode()) == 6
    assert p.bytes <= 10


def test_known_key_masked_and_redacted_flag(db):
    lid = LedgerWriter(db).record(
        _att(request_text=json.dumps({"auth": f"Bearer {KEY}", "x": KEY}), response_text=f"echo {KEY}")
    )
    p = repo.get_payload(db, lid)
    assert KEY not in p.request_json and KEY not in p.response_text
    assert MASK in p.request_json and MASK in p.response_text
    assert p.redacted is True


def test_redacted_flag_from_response_only(db):
    lid = LedgerWriter(db).record(_att(request_text="clean", response_text=f"leak {KEY}"))
    p = repo.get_payload(db, lid)
    assert p.redacted is True and KEY not in p.response_text


def test_bytes_column_is_size_after_redaction(db):
    lid = LedgerWriter(db).record(_att(request_text=KEY, response_text=None))
    p = repo.get_payload(db, lid)
    assert p.request_json == MASK and p.bytes == len(MASK.encode())


def _no_key_fragment(text: str, key: str) -> None:
    assert key not in text
    # 截斷後的尾端不可是金鑰的任何前綴片段
    for i in range(1, len(key) + 1):
        assert not text.endswith(key[:i]), (text, key[:i])
    assert "sk-" not in text


@pytest.mark.parametrize("cut_into_key", range(1, len(KEY)))
def test_key_across_truncation_boundary_leaves_no_prefix(db, cut_into_key):
    # 截斷點落在金鑰第 cut_into_key 個字元之後(若先截斷再遮罩,會留下 "sk-te" 這類前綴)
    prefix = "p" * 20 + " "
    repo.set_setting(db, "context_max_bytes", 2 * (len(prefix) + cut_into_key))
    text = prefix + KEY + " tail"
    lid = LedgerWriter(db).record(_att(request_text=text, response_text=text))
    p = repo.get_payload(db, lid)
    assert p.truncated is True
    _no_key_fragment(p.request_json, KEY)
    _no_key_fragment(p.response_text, KEY)


@pytest.mark.parametrize("cut_into_key", [1, 4, 9])  # 需小於 len(MASK) 才會截斷
def test_extra_secret_across_truncation_boundary_leaves_no_prefix(db, cut_into_key):
    secret = "ZZsecretVALUE-0042"
    prefix = "q" * 30 + "="
    repo.set_setting(db, "context_max_bytes", len(prefix) + cut_into_key)
    lid = LedgerWriter(db).record(
        _att(request_text=prefix + secret + "!!", response_text=None, extra_secrets=(secret,))
    )
    p = repo.get_payload(db, lid)
    assert p.truncated and p.redacted
    _no_key_fragment(p.request_json, secret)


def test_error_masked_with_extra_secrets(db):
    secret = "plain-credential-XYZ123"
    err = f"401 invalid key {secret}; Authorization: Bearer {KEY}"
    lid = LedgerWriter(db).record(_att(status="error", error=err, extra_secrets=(secret,)))
    stored = repo.get_ledger(db, lid).error
    assert secret not in stored and KEY not in stored
    assert MASK in stored and stored.startswith("401 invalid key ")


def test_extra_secrets_masked_in_payload(db):
    secret = "plain-credential-XYZ123"
    lid = LedgerWriter(db).record(
        _att(request_text=f"k={secret}", response_text=f"r={secret}", extra_secrets=[secret])
    )
    p = repo.get_payload(db, lid)
    assert secret not in p.request_json and secret not in p.response_text and p.redacted


def test_custom_redactor_used(db):
    calls = []

    class Spy:
        def redact(self, text, extra_secrets=()):
            calls.append(text)
            return text.upper(), False

    lid = LedgerWriter(db, redactor=Spy()).record(_att(error="boom"))
    assert repo.get_ledger(db, lid).error == "BOOM"
    assert repo.get_payload(db, lid).response_text == "HI THERE"
    assert len(calls) == 3


# ---------------------------------------------------------------- L2:是否寫 payload


def test_route_record_context_zero_skips_payload(db):
    lid = LedgerWriter(db).record(_att(route_record_context=0))
    assert repo.get_ledger(db, lid) is not None
    assert _payload_row(db, lid) is None


def test_route_record_context_one_overrides_global_off(db):
    repo.set_setting(db, "record_context_default", False)
    lid = LedgerWriter(db).record(_att(route_record_context=1))
    assert _payload_row(db, lid) is not None


@pytest.mark.parametrize("global_on", [True, False])
def test_route_record_context_null_follows_global(db, global_on):
    repo.set_setting(db, "record_context_default", global_on)
    lid = LedgerWriter(db).record(_att(route_record_context=None))
    assert (_payload_row(db, lid) is not None) is global_on


def test_route_record_context_from_db_route(db):
    # 雙側:route 表存的 record_context 經 repo 讀回後交給 Attempt
    repo.save_route(db, repo.Route(name="off", target={"group": "g"}, record_context=False))
    repo.save_route(db, repo.Route(name="inherit", target={"group": "g"}, record_context=None))
    w = LedgerWriter(db)
    off = w.record(_att(route_name="off", route_record_context=repo.get_route(db, "off").record_context))
    inh = w.record(_att(route_name="inherit", route_record_context=repo.get_route(db, "inherit").record_context))
    assert _payload_row(db, off) is None
    assert _payload_row(db, inh) is not None


def test_local_direct_call_uses_global(db):
    w = LedgerWriter(db)
    lid = w.record(_att(route_name=None, slot=None, route_record_context=None))
    assert _payload_row(db, lid) is not None
    repo.set_setting(db, "record_context_default", False)
    lid2 = w.record(_att(route_name=None, slot=None, route_record_context=None))
    assert _payload_row(db, lid2) is None


def test_retention_zero_writes_no_payload_even_if_route_on(db):
    repo.set_setting(db, "context_retention_days", 0)
    lid = LedgerWriter(db).record(_att(route_record_context=1))
    assert repo.get_ledger(db, lid) is not None
    assert _payload_row(db, lid) is None
    assert payload_status(db, lid, T0) == "never_recorded"


def test_no_request_text_writes_no_payload(db):
    lid = LedgerWriter(db).record(_att(status="skipped_precheck", request_text=None, response_text=None))
    assert repo.get_ledger(db, lid).status == "skipped_precheck"
    assert _payload_row(db, lid) is None


def test_ledger_and_payload_same_transaction(db, monkeypatch):
    def boom(conn, p):
        raise RuntimeError("payload insert failed")

    monkeypatch.setattr(repo, "insert_payload", boom)
    with pytest.raises(RuntimeError):
        LedgerWriter(db).record(_att())
    assert db.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0] == 0


def test_each_attempt_own_row(db):
    w = LedgerWriter(db)
    ids = [w.record(_att(status="error", fallback_depth=i, slot=i)) for i in range(3)]
    assert len(set(ids)) == 3
    assert db.execute("SELECT COUNT(*) FROM usage_payloads").fetchone()[0] == 3


# ---------------------------------------------------------------- L2:保存與狀態


def test_retention_advance_8_days(db):
    clock = FakeClock(T0)
    w = LedgerWriter(db)
    old = w.record(_att(ts=clock.now()))
    old_off = w.record(_att(ts=clock.now(), route_record_context=0))
    clock.advance(days=8)
    new = w.record(_att(ts=clock.now()))
    new_off = w.record(_att(ts=clock.now(), route_record_context=0))

    assert payload_status(db, old, clock.now()) == "ok"  # 尚未清理前仍存在
    deleted = RetentionJob(db, clock).run_once(clock.now())
    assert deleted == 1
    assert _payload_row(db, old) is None
    assert _payload_row(db, new) is not None
    assert repo.get_ledger(db, old) is not None  # 帳本列保留

    assert payload_status(db, old, clock.now()) == "expired"
    assert w.payload_status(new, clock.now()) == "ok"
    assert payload_status(db, new_off, clock.now()) == "never_recorded"
    # 當初未記錄但已超過期限:以 ts 判斷,回 expired(決策 17 的已知限制)
    assert payload_status(db, old_off, clock.now()) == "expired"


def test_retention_run_once_defaults_to_clock_and_keeps_boundary(db):
    clock = FakeClock(T0)
    w = LedgerWriter(db)
    at_boundary = w.record(_att(ts=clock.now()))
    clock.advance(days=7)
    assert RetentionJob(db, clock).run_once() == 0  # ts == 期限,不早於期限
    assert _payload_row(db, at_boundary) is not None
    assert payload_status(db, at_boundary, clock.now()) == "ok"
    clock.advance(seconds=1)
    assert RetentionJob(db, clock).run_once() == 1
    assert payload_status(db, at_boundary, clock.now()) == "expired"


def test_retention_uses_setting(db):
    clock = FakeClock(T0)
    repo.set_setting(db, "context_retention_days", 2)
    w = LedgerWriter(db)
    lid = w.record(_att(ts=clock.now()))
    clock.advance(days=3)
    assert RetentionJob(db, clock).run_once() == 1
    assert payload_status(db, lid, clock.now()) == "expired"


def test_payload_status_unknown_ledger_raises(db):
    with pytest.raises(LedgerNotFound):
        payload_status(db, 9999, T0)


def test_deleting_ledger_cascades_payload(db):
    lid = LedgerWriter(db).record(_att())
    assert _payload_row(db, lid) is not None
    db.execute("DELETE FROM usage_ledger WHERE id = ?", (lid,))
    assert _payload_row(db, lid) is None
    assert db.execute("SELECT COUNT(*) FROM usage_payloads").fetchone()[0] == 0

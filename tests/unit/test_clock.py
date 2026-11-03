from datetime import datetime, timezone

import pytest

from spillway.clock import FakeClock, SeqRng, SystemRng, from_iso, to_iso


def test_fake_clock_advance_is_exact_and_utc():
    c = FakeClock(datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc))
    before = c.now()
    after = c.advance(days=8)
    assert after - before == (datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc) - before)
    assert after.tzinfo == timezone.utc
    assert c.now() == after


def test_fake_clock_rejects_naive_time():
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 10, 30))


def test_system_rng_seed_is_reproducible():
    a = [SystemRng(seed=1).random() for _ in range(1)]
    r1, r2 = SystemRng(seed=1), SystemRng(seed=1)
    assert [r1.random() for _ in range(5)] == [r2.random() for _ in range(5)]
    assert 0 <= a[0] < 1


def test_seq_rng_cycles():
    r = SeqRng([0.1, 0.9])
    assert [r.random() for _ in range(3)] == [0.1, 0.9, 0.1]
    with pytest.raises(ValueError):
        SeqRng([])


def test_iso_roundtrip_to_seconds():
    dt = datetime(2026, 10, 30, 12, 0, 5, 123456, tzinfo=timezone.utc)
    s = to_iso(dt)
    assert s == "2026-10-30T12:00:05Z"
    assert from_iso(s) == dt.replace(microsecond=0)


def test_naive_time_is_rejected_everywhere():
    c = FakeClock()
    with pytest.raises(ValueError):
        c.set(datetime(2026, 1, 1, 12))
    with pytest.raises(ValueError):
        to_iso(datetime(2026, 1, 1, 12))


# ---- CHG-20261103-01 T1(決策 21 ⑤):utc_strftime、utc_stamp ----------------------------------

def test_utc_stamp_formats_utc_seconds():
    from spillway.clock import utc_stamp
    assert utc_stamp(datetime(2026, 11, 3, 1, 13, 20, tzinfo=timezone.utc)) == "20261103T011320Z"


def test_utc_stamp_converts_non_utc_input_first():
    from datetime import timedelta
    from spillway.clock import utc_stamp
    taipei = timezone(timedelta(hours=8))
    assert utc_stamp(datetime(2026, 11, 3, 9, 13, 20, tzinfo=taipei)) == "20261103T011320Z"


def test_utc_strftime_applies_any_format_in_utc():
    from datetime import timedelta
    from spillway.clock import utc_strftime
    minus5 = timezone(timedelta(hours=-5))
    now = datetime(2026, 11, 2, 22, 30, 5, tzinfo=minus5)
    assert utc_strftime(now, "x-%Y-%m-%d %H:%M:%S.csv") == "x-2026-11-03 03:30:05.csv"
    assert utc_strftime(now, "spillway-config-%Y%m%dT%H%M%SZ.yaml") == "spillway-config-20261103T033005Z.yaml"


def test_configio_utc_stamp_filename_is_clock_alias():
    from spillway import clock, configio
    assert configio.utc_stamp_filename is clock.utc_strftime
    assert "utc_stamp_filename" in configio.__all__

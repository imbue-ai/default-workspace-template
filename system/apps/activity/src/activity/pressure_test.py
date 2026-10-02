from datetime import datetime
from datetime import timezone

from activity.history import MemorySample
from activity.pressure import RECENT_SECONDS
from activity.pressure import latest_pressure

_TIGHT = 5_000
_NOW = 1_000_000


def _readings(start: int, used_by_minute: list[int]) -> list[MemorySample]:
    return [
        MemorySample(at_epoch_seconds=start + 60 * minute, used_kib=used, limit_kib=8_000)
        for minute, used in enumerate(used_by_minute)
    ]


def test_five_tight_minutes_up_to_now_are_ongoing_pressure_with_their_start_and_peak() -> None:
    samples = _readings(_NOW - 60 * 6, [4_000, 5_100, 5_300, 5_900, 5_200, 5_000, 5_400])
    pressure = latest_pressure(samples, _TIGHT, _NOW)
    assert pressure is not None
    assert pressure.is_ongoing is True
    assert pressure.started_at == datetime.fromtimestamp(_NOW - 60 * 5, tz=timezone.utc)
    assert pressure.last_tight_at == datetime.fromtimestamp(_NOW, tz=timezone.utc)
    assert pressure.peak_kib == 5_900


def test_a_short_spike_is_not_pressure() -> None:
    assert latest_pressure(_readings(_NOW - 60 * 4, [4_000, 6_000, 6_000, 6_000, 4_000]), _TIGHT, _NOW) is None


def test_one_reading_below_the_line_or_a_gap_in_recording_ends_a_stretch() -> None:
    dipped = _readings(_NOW - 60 * 6, [5_500, 5_500, 5_500, 4_900, 5_500, 5_500, 5_500])
    assert latest_pressure(dipped, _TIGHT, _NOW) is None
    gapped = _readings(_NOW - 60 * 9, [5_500, 5_500, 5_500]) + _readings(_NOW - 60 * 2, [5_500, 5_500, 5_500])
    assert latest_pressure(gapped, _TIGHT, _NOW) is None


def test_a_stretch_that_ended_is_still_mentioned_for_an_hour_then_dropped() -> None:
    ended = _readings(_NOW - 60 * 30, [5_500] * 10 + [4_000] * 21)
    recent = latest_pressure(ended, _TIGHT, _NOW)
    assert recent is not None and recent.is_ongoing is False
    assert recent.last_tight_at == datetime.fromtimestamp(_NOW - 60 * 21, tz=timezone.utc)
    assert latest_pressure(ended, _TIGHT, _NOW + RECENT_SECONDS) is None


def test_a_stretch_whose_recording_stopped_is_not_called_ongoing() -> None:
    stale = _readings(_NOW - 60 * 15, [5_500] * 6) + _readings(_NOW, [4_000])
    pressure = latest_pressure(stale, _TIGHT, _NOW)
    assert pressure is not None and pressure.is_ongoing is False


def test_a_missed_reading_ends_a_stretch_though_the_next_one_is_tight_too() -> None:
    # Two minutes between readings means one was missed: three readings, a gap, three readings is not five minutes.
    gapped = _readings(_NOW - 60 * 6, [5_500, 5_500, 5_500]) + _readings(_NOW - 60 * 2, [5_500, 5_500, 5_500])
    assert latest_pressure(gapped, _TIGHT, _NOW) is None


def test_five_readings_in_a_row_warn_and_four_do_not() -> None:
    assert latest_pressure(_readings(_NOW - 60 * 4, [5_500] * 5), _TIGHT, _NOW) is not None
    assert latest_pressure(_readings(_NOW - 60 * 3, [5_500] * 4), _TIGHT, _NOW) is None
    exactly_at_the_line = _readings(_NOW - 60 * 4, [_TIGHT] * 5)
    assert latest_pressure(exactly_at_the_line, _TIGHT, _NOW) is not None


def test_an_eased_stretch_is_not_mentioned_while_memory_is_tight_again() -> None:
    eased_then_tight = _readings(_NOW - 60 * 20, [5_500] * 6 + [4_000] * 12 + [5_500] * 3)
    assert latest_pressure(eased_then_tight, _TIGHT, _NOW) is None


def test_no_readings_is_no_pressure_and_order_does_not_matter() -> None:
    assert latest_pressure([], _TIGHT, _NOW) is None
    shuffled = list(reversed(_readings(_NOW - 60 * 5, [5_500] * 6)))
    pressure = latest_pressure(shuffled, _TIGHT, _NOW)
    assert pressure is not None and pressure.is_ongoing is True

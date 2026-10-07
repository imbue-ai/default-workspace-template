from pathlib import Path

from activity.history import MemorySample
from activity.history import REWRITE_ABOVE_BYTES
from activity.history import append_sample
from activity.history import format_sample
from activity.history import group_into_periods
from activity.history import is_recording
from activity.history import parse_history
from activity.history import read_history
from activity.memory_reading import MemorySource


def _sample(at: int, used: int) -> MemorySample:
    return MemorySample(at_epoch_seconds=at, used_kib=used, limit_kib=8_000_000)


def test_a_reading_round_trips_and_a_malformed_or_half_written_line_is_skipped() -> None:
    text = format_sample(_sample(100, 2_000)) + "garbage\n" + "200\t3000\n" + format_sample(_sample(160, 2_500))
    assert parse_history(text) == [_sample(100, 2_000), _sample(160, 2_500)]


def test_a_missing_file_is_no_readings_yet(tmp_path: Path) -> None:
    assert read_history(tmp_path / "missing.tsv") == []


def test_corrupt_bytes_cost_only_their_own_line_and_an_unreadable_file_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "memory-history.tsv"
    path.write_bytes(
        format_sample(_sample(100, 1)).encode()
        + b"1\xff0\t2\t3\n\xb2\t2\t3\n"
        + format_sample(_sample(160, 2)).encode()
    )
    assert read_history(path) == [_sample(100, 1), _sample(160, 2)]
    assert read_history(tmp_path) == []


def test_readings_append_and_old_ones_are_dropped_once_the_file_grows_past_its_bound(tmp_path: Path) -> None:
    path = tmp_path / "state" / "memory-history.tsv"
    append_sample(path, _sample(1_000, 1), retention_seconds=500)
    append_sample(path, _sample(1_060, 2), retention_seconds=500)
    assert read_history(path) == [_sample(1_000, 1), _sample(1_060, 2)]

    old_line = format_sample(_sample(10, 9))
    padding = old_line * (REWRITE_ABOVE_BYTES // len(old_line) + 10)
    path.write_text(padding + path.read_text())
    append_sample(path, _sample(1_120, 3), retention_seconds=500)
    assert read_history(path) == [_sample(1_000, 1), _sample(1_060, 2), _sample(1_120, 3)]


def test_periods_keep_the_typical_reading_and_its_range_so_a_spike_survives_grouping() -> None:
    samples = [_sample(0, 100), _sample(60, 100), _sample(120, 900), _sample(240, 200), _sample(4_000, 50)]
    periods = group_into_periods(samples, end_epoch_seconds=300, span_seconds=300, period_seconds=180)
    assert [(p.start_epoch_seconds, p.average_kib, p.min_kib, p.max_kib, p.sample_count) for p in periods] == [
        (0, 366, 100, 900, 3),
        (180, 200, 200, 200, 1),
    ]


def test_a_gap_in_recording_stays_a_gap() -> None:
    periods = group_into_periods(
        [_sample(0, 1), _sample(600, 2)], end_epoch_seconds=600, span_seconds=600, period_seconds=60
    )
    assert [p.start_epoch_seconds for p in periods] == [0, 600]


def test_recording_means_a_reading_within_the_last_few_minutes() -> None:
    assert is_recording([_sample(1_000, 1)], now_epoch_seconds=1_100) is True
    assert is_recording([_sample(1_000, 1)], now_epoch_seconds=1_500) is False
    assert is_recording([], now_epoch_seconds=1_000) is False


def test_a_reading_keeps_its_source_and_lines_from_before_or_after_this_format_still_parse() -> None:
    with_source = MemorySample(at_epoch_seconds=100, used_kib=5, limit_kib=9, source=MemorySource.CGROUP)
    assert format_sample(with_source) == "100\t5\t9\tCGROUP\n"
    text = (
        format_sample(with_source)
        + "160\t6\t9\n"
        + "220\t7\t9\tHOST_MEMINFO\tsome-later-column\n"
        + "280\t8\t9\tMARS\n"
    )
    assert [(sample.at_epoch_seconds, sample.source) for sample in parse_history(text)] == [
        (100, MemorySource.CGROUP),
        (160, None),
        (220, MemorySource.HOST_MEMINFO),
        (280, None),
    ]


def test_recording_is_judged_by_the_newest_reading_whatever_the_file_order() -> None:
    assert is_recording([_sample(1_000, 1), _sample(400, 1)], now_epoch_seconds=1_100) is True

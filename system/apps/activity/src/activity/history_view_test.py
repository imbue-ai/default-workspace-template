from datetime import datetime
from datetime import timezone
from pathlib import Path

from activity.closures import describe_closure
from activity.history import HistoryRange
from activity.history import MemorySample
from activity.history_view import build_history_view
from activity.memory_reading import ClosingPoint
from activity.memory_reading import MemoryCloser

_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
_END = int(_NOW.timestamp())


def _shed(timestamp: str) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "type": "process_shed",
        "pid": 1,
        "comm": "pytest",
        "agent_name": None,
        "vm_rss_kib": 9,
    }


def test_the_view_holds_the_ranges_periods_the_closing_line_and_closures_in_and_beyond_the_span() -> None:
    samples = [
        MemorySample(at_epoch_seconds=_END - offset, used_kib=1_000, limit_kib=8_000) for offset in (7200, 600, 60)
    ]
    in_hour = describe_closure(_shed("2026-10-01T11:30:00.000000Z"))
    earlier = describe_closure(_shed("2026-10-01T08:00:00.000000Z"))
    assert in_hour is not None and earlier is not None
    closing = ClosingPoint(
        used_bytes=7_000 * 1024,
        closer=MemoryCloser.MEMORY_GUARD,
        min_available_percent=10,
        badness_total_kib=8_000,
        detail="",
    )
    view = build_history_view(
        history_range=HistoryRange.HOUR,
        samples=samples,
        closures=[in_hour, earlier],
        closing=closing,
        limit_bytes=8_000 * 1024,
        now=_NOW,
        history_path=Path("data/.state/activity/memory-history.tsv"),
        ledger_path=Path("data/.state/oom_priority/events/shed.jsonl"),
    )
    assert (view.span_seconds, view.period_seconds) == (3600, 60)
    assert len(view.periods) == 2
    assert (view.limit_kib, view.closes_at_kib, view.closer) == (8_000, 7_000, MemoryCloser.MEMORY_GUARD)
    assert [closure.at.hour for closure in view.closures_in_range] == [11]
    assert len(view.recent_closures) == 2
    assert view.is_recording is True
    assert view.first_sample_epoch_seconds == _END - 7200


def test_with_nothing_recorded_the_view_says_so() -> None:
    view = build_history_view(
        history_range=HistoryRange.WEEK,
        samples=[],
        closures=[],
        closing=None,
        limit_bytes=None,
        now=_NOW,
        history_path=Path("h"),
        ledger_path=Path("l"),
    )
    assert (view.periods, view.is_recording, view.first_sample_epoch_seconds, view.closes_at_kib) == (
        (),
        False,
        None,
        None,
    )

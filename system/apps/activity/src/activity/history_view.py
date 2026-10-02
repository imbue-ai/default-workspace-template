"""The memory-over-time chart's document: the periods of the chosen range, the closing line, and what was closed.

Read when asked, like everything else the app serves: the readings file the recorder appends to, the shed ledger, and
one fresh memory reading for the closing line.
"""

from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Final

from pydantic import Field

from activity.closures import ClosedProcess
from activity.closures import closures_since
from activity.history import HistoryPeriod
from activity.history import HistoryRange
from activity.history import MemorySample
from activity.history import RANGE_SPANS
from activity.history import RETENTION_SECONDS
from activity.history import epoch_seconds
from activity.history import group_into_periods
from activity.history import is_recording
from activity.history import read_history
from activity.memory_reading import BYTES_PER_KIB
from activity.memory_reading import ClosingPoint
from activity.memory_reading import MemoryCloser
from activity.memory_reading import MemorySources
from activity.memory_reading import closing_point
from activity.memory_reading import read_earlyoom_meminfo
from activity.memory_reading import read_memory
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from oom_priority.badness import find_earlyoom_argv
from oom_priority.ledger import read_records

RECENT_CLOSURE_LIMIT: Final[int] = 20


class HistoryView(FrozenModel):
    """Everything the chart and the "Recently closed" list show."""

    range: HistoryRange = Field(description="The span shown")
    span_seconds: int = Field(description="Its length")
    period_seconds: int = Field(description="The length of one period on it")
    end_epoch_seconds: int = Field(description="When the span ends: now")
    periods: tuple[HistoryPeriod, ...] = Field(description="Its periods that hold readings, oldest first")
    limit_kib: int | None = Field(description="The memory limit, in KiB, when known")
    closes_at_kib: int | None = Field(description="Where closing starts, in KiB, when known")
    closer: MemoryCloser | None = Field(description="Who closes things there, when known")
    closures_in_range: tuple[ClosedProcess, ...] = Field(description="What was closed within the span, newest first")
    recent_closures: tuple[ClosedProcess, ...] = Field(description="What was closed in the last week, newest first")
    is_recording: bool = Field(description="Whether the recorder has written within the last few minutes")
    first_sample_epoch_seconds: int | None = Field(description="When the oldest kept reading was taken")
    history_path: str = Field(description="The readings file, for the details")
    ledger_path: str = Field(description="The shed ledger, for the details")


@pure
def build_history_view(
    history_range: HistoryRange,
    samples: Sequence[MemorySample],
    closures: Sequence[ClosedProcess],
    closing: ClosingPoint | None,
    limit_bytes: int | None,
    now: datetime,
    history_path: Path,
    ledger_path: Path,
) -> HistoryView:
    span_seconds, period_seconds = RANGE_SPANS[history_range]
    end = epoch_seconds(now)
    range_start = now - timedelta(seconds=span_seconds)
    return HistoryView(
        range=history_range,
        span_seconds=span_seconds,
        period_seconds=period_seconds,
        end_epoch_seconds=end,
        periods=tuple(group_into_periods(samples, end, span_seconds, period_seconds)),
        limit_kib=limit_bytes // BYTES_PER_KIB if limit_bytes is not None else None,
        closes_at_kib=closing.used_bytes // BYTES_PER_KIB if closing is not None else None,
        closer=closing.closer if closing is not None else None,
        closures_in_range=tuple(closure for closure in closures if closure.at >= range_start),
        recent_closures=tuple(closures[:RECENT_CLOSURE_LIMIT]),
        is_recording=is_recording(samples, end),
        first_sample_epoch_seconds=samples[0].at_epoch_seconds if samples else None,
        history_path=str(history_path),
        ledger_path=str(ledger_path),
    )


def collect_history_view(
    history_range: HistoryRange,
    memory_sources: MemorySources,
    proc_dir: Path,
    history_path: Path,
    ledger_path: Path,
    now: datetime,
) -> HistoryView:
    reading = read_memory(memory_sources)
    earlyoom = find_earlyoom_argv(proc_dir) if proc_dir.is_dir() else None
    closing = (
        closing_point(reading, read_earlyoom_meminfo(memory_sources), tuple(earlyoom[1]) if earlyoom else None)
        if reading is not None
        else None
    )
    week_ago = now - timedelta(seconds=RETENTION_SECONDS)
    return build_history_view(
        history_range=history_range,
        samples=read_history(history_path),
        closures=closures_since(read_records(ledger_path), week_ago),
        closing=closing,
        limit_bytes=reading.limit_bytes if reading is not None else None,
        now=now,
        history_path=history_path,
        ledger_path=ledger_path,
    )

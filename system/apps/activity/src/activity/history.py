"""Memory over time: the once-a-minute readings the recorder appends, grouped into the periods a chart shows.

The recorder (``record_memory``) appends one line per minute, ``<epoch seconds>\\t<used KiB>\\t<limit KiB>``, to a
small file under data/.state/ and keeps a week of them. A period of the chart keeps the typical (mean) reading and the
lowest and highest one in it, so a short spike still shows at the coarser ranges. Readings are a minute apart: a spike
shorter than that may not be in any of them, and the page says so.
"""

import os
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

HISTORY_PATH: Final[Path] = Path("data/.state/activity/memory-history.tsv")
RETENTION_SECONDS: Final[int] = 7 * 24 * 3600
# Rewriting the file to drop old readings is only worth it once it has grown well past a week's worth (about
# 300 KB); until then a reading is a single append.
REWRITE_ABOVE_BYTES: Final[int] = 512 * 1024
# A recorder that has written within this long is recording; a minute's cron plus slack.
RECORDING_FRESH_SECONDS: Final[int] = 3 * 60


class HistoryRange(UpperCaseStrEnum):
    """The spans the chart offers."""

    HOUR = auto()
    DAY = auto()
    WEEK = auto()


# Each range's span and the length of one period on it.
RANGE_SPANS: Final[dict[HistoryRange, tuple[int, int]]] = {
    HistoryRange.HOUR: (3600, 60),
    HistoryRange.DAY: (24 * 3600, 5 * 60),
    HistoryRange.WEEK: (7 * 24 * 3600, 30 * 60),
}


class MemorySample(FrozenModel):
    """One reading the recorder took."""

    at_epoch_seconds: int = Field(description="When it was taken")
    used_kib: int = Field(description="Memory in use, in KiB")
    limit_kib: int = Field(description="The limit at the time, in KiB")


class HistoryPeriod(FrozenModel):
    """One period of the chart: its typical reading and its range."""

    start_epoch_seconds: int = Field(description="When the period starts")
    average_kib: int = Field(description="The mean of its readings, in KiB")
    min_kib: int = Field(description="Its lowest reading, in KiB")
    max_kib: int = Field(description="Its highest reading, in KiB")
    sample_count: int = Field(description="How many readings it holds")


@pure
def format_sample(sample: MemorySample) -> str:
    return f"{sample.at_epoch_seconds}\t{sample.used_kib}\t{sample.limit_kib}\n"


@pure
def parse_history(text: str) -> list[MemorySample]:
    """Every well-formed line, in file order; a malformed or half-written line is skipped."""
    samples: list[MemorySample] = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) == 3 and all(field.isascii() and field.isdigit() for field in fields):
            samples.append(
                MemorySample(at_epoch_seconds=int(fields[0]), used_kib=int(fields[1]), limit_kib=int(fields[2]))
            )
    return samples


def read_history(path: Path) -> list[MemorySample]:
    """Every reading in the file; none when it is missing or unreadable, and corrupt bytes only cost their lines."""
    try:
        text = path.read_text(errors="replace")
    except FileNotFoundError:
        return []
    except OSError as e:
        logger.warning("Could not read the memory history at {}: {}", path, e)
        return []
    return parse_history(text)


def append_sample(path: Path, sample: MemorySample, retention_seconds: int) -> None:
    """Add a reading; once the file has grown past a week's worth, rewrite it without the readings that aged out."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as history_file:
        history_file.write(format_sample(sample))
    if path.stat().st_size <= REWRITE_ABOVE_BYTES:
        return
    cutoff = sample.at_epoch_seconds - retention_seconds
    kept = [kept_sample for kept_sample in read_history(path) if kept_sample.at_epoch_seconds >= cutoff]
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary_path.write_text("".join(format_sample(kept_sample) for kept_sample in kept))
    os.replace(temporary_path, path)


@pure
def group_into_periods(
    samples: Sequence[MemorySample], end_epoch_seconds: int, span_seconds: int, period_seconds: int
) -> list[HistoryPeriod]:
    """The readings of the span ending now, grouped into periods aligned to the period length; empty periods are
    left out, so a gap in recording reads as a gap."""
    start = end_epoch_seconds - span_seconds
    grouped: dict[int, list[int]] = {}
    for sample in samples:
        if start <= sample.at_epoch_seconds <= end_epoch_seconds:
            period_start = sample.at_epoch_seconds - sample.at_epoch_seconds % period_seconds
            grouped.setdefault(period_start, []).append(sample.used_kib)
    return [
        HistoryPeriod(
            start_epoch_seconds=period_start,
            average_kib=sum(values) // len(values),
            min_kib=min(values),
            max_kib=max(values),
            sample_count=len(values),
        )
        for period_start, values in sorted(grouped.items())
    ]


@pure
def is_recording(samples: Sequence[MemorySample], now_epoch_seconds: int) -> bool:
    return bool(samples) and now_epoch_seconds - samples[-1].at_epoch_seconds <= RECORDING_FRESH_SECONDS


def epoch_seconds(moment: datetime) -> int:
    return int(moment.astimezone(timezone.utc).timestamp())

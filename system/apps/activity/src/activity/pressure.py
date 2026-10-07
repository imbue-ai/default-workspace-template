"""Sustained memory pressure, from the recorder's minute-by-minute history: whether memory has stayed at or above
"getting tight" long enough to warn about, and since when.

Worked out when the page asks rather than tracked by the recorder, so there is no alert state to go stale: one rule
over the readings the chart already shows. A gap in recording, or one reading below the line, ends a stretch, so a
warning always describes memory that was tight minute after minute.
"""

from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from typing import Final

from pydantic import Field

from activity.history import MemorySample
from activity.history import RECORDING_FRESH_SECONDS
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

# Five readings a minute apart (four minutes from first to last, each standing for its own minute): five minutes
# of pressure, not a passing spike.
SUSTAINED_SECONDS: Final[int] = 4 * 60
# A cron minute can start a few seconds late; a longer gap means a reading was missed, which ends a stretch.
MAX_READING_GAP_SECONDS: Final[int] = 90
# Readings older than this are not read for the warning; a stretch longer than it reads as starting this long ago.
PRESSURE_LOOKBACK_SECONDS: Final[int] = 2 * 24 * 3600
# How long after a stretch ends the page still mentions it.
RECENT_SECONDS: Final[int] = 60 * 60


class PressureStretch(FrozenModel):
    """The latest stretch of readings at or above "getting tight" that lasted long enough to warn about."""

    started_at: datetime = Field(description="The first reading of the stretch")
    last_tight_at: datetime = Field(description="The last reading of the stretch")
    peak_kib: int = Field(description="The highest reading in it, in KiB")
    is_ongoing: bool = Field(description="Whether the latest reading, taken just now, is still part of it")


@pure
def latest_pressure(
    samples: Sequence[MemorySample], tight_from_kib: int, now_epoch_seconds: int
) -> PressureStretch | None:
    """The latest sustained stretch, while it lasts and for an hour after; None when there is none to mention."""
    ordered = sorted(samples, key=lambda sample: sample.at_epoch_seconds)
    stretches: list[list[MemorySample]] = []
    is_stretch_open = False
    for sample in ordered:
        if sample.used_kib < tight_from_kib:
            is_stretch_open = False
            continue
        previous = stretches[-1][-1] if stretches and is_stretch_open else None
        if previous is not None and sample.at_epoch_seconds - previous.at_epoch_seconds <= MAX_READING_GAP_SECONDS:
            stretches[-1].append(sample)
        else:
            stretches.append([sample])
        is_stretch_open = True
    sustained = [
        stretch
        for stretch in stretches
        if stretch[-1].at_epoch_seconds - stretch[0].at_epoch_seconds >= SUSTAINED_SECONDS
    ]
    if not sustained:
        return None
    stretch = sustained[-1]
    last_at = stretch[-1].at_epoch_seconds
    is_ongoing = stretch[-1] is ordered[-1] and now_epoch_seconds - last_at <= RECORDING_FRESH_SECONDS
    if not is_ongoing and now_epoch_seconds - last_at > RECENT_SECONDS:
        return None
    # An eased stretch is not mentioned while the latest reading is tight again: that is a new stretch, too short yet
    # to warn about, and "it was tight earlier" beside a tight headline would contradict it.
    if not is_ongoing and ordered[-1].used_kib >= tight_from_kib:
        return None
    return PressureStretch(
        started_at=datetime.fromtimestamp(stretch[0].at_epoch_seconds, tz=timezone.utc),
        last_tight_at=datetime.fromtimestamp(last_at, tz=timezone.utc),
        peak_kib=max(sample.used_kib for sample in stretch),
        is_ongoing=is_ongoing,
    )

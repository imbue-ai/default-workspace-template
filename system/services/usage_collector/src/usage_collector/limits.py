"""Subscription limit readings: how much of each rate-limit window a provider's subscription has used.

A harness running on a subscription (Claude Pro/Max, a ChatGPT plan through Codex) reports its account's
windows -- ``five_hour``, ``seven_day`` and so on -- on the usage events it writes: the agents' snapshot events
(``{"source": "claude/usage", "timestamp": ..., "rate_limits": {...}}``) and, when the harness passes them
through, a model_call's ``rate_limits``. A window is an account-level counter, so the freshest reading per
source and window is the answer; ``rate_limits`` is null or absent for a harness on an API key.
"""

from collections.abc import Mapping
from datetime import datetime, timezone

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_usage.data_types import WindowSnapshot
from loguru import logger
from pydantic import Field, ValidationError


class LimitReading(FrozenModel):
    """One window of one source's subscription, as last reported."""

    source: str = Field(description="The harness that reported it: claude, codex, ...")
    window: str = Field(description="The window's key: five_hour, seven_day, ...")
    label: str | None = Field(
        default=None, description="The writer's short label: 5h, 7d, ..."
    )
    used_percentage: float | None = Field(default=None)
    resets_at: datetime | None = Field(
        default=None, description="When the window resets"
    )
    window_seconds: int | None = Field(
        default=None, description="The window's length, when fixed"
    )
    observed_at: datetime = Field(
        description="When the event carrying this reading was written"
    )
    first_used_percentage: float | None = Field(
        default=None,
        description="The lowest share seen in this window: where this workspace's calls began to count in it",
    )
    first_observed_at: datetime | None = Field(
        default=None,
        description="When the lowest share was first seen; the calls after it are what the window's rise is from",
    )


def _source_of(record: Mapping[str, object]) -> str | None:
    source = record.get("source")
    if isinstance(source, str) and source.strip():
        return source.split("/", 1)[0]
    attribution = record.get("attribution")
    if isinstance(attribution, Mapping):
        harness = attribution.get("harness")
        if isinstance(harness, str) and harness.strip():
            return harness
    return None


def _observed_at(record: Mapping[str, object]) -> datetime | None:
    value = record.get("timestamp") or record.get("ended_at")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def limit_readings(record: object) -> list[LimitReading]:
    """The limit readings an event carries; none for an event without rate-limit windows."""
    if not isinstance(record, Mapping):
        return []
    windows = record.get("rate_limits")
    if not isinstance(windows, Mapping) or not windows:
        return []
    source = _source_of(record)
    observed_at = _observed_at(record)
    if source is None or observed_at is None:
        return []
    readings = []
    for window, raw in windows.items():
        try:
            snapshot = WindowSnapshot.model_validate(raw)
        except ValidationError as e:
            logger.warning(
                "Skipped an unreadable {} window from {}: {}", window, source, e
            )
            continue
        readings.append(
            LimitReading(
                source=source,
                window=str(window),
                label=snapshot.label,
                used_percentage=snapshot.used_percentage,
                resets_at=(
                    datetime.fromtimestamp(snapshot.resets_at, tz=timezone.utc)
                    if snapshot.resets_at is not None
                    else None
                ),
                window_seconds=snapshot.window_seconds,
                observed_at=observed_at,
            )
        )
    return readings

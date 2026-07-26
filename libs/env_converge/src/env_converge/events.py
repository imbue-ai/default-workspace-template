"""Structured events for environment capture / convergence / upgrade.

Events land at `$MNGR_HOST_DIR/plugin/env-converge/events/env_converge/events.jsonl`
(host-level, not per-agent: the environment is a host-wide fact), one JSONL
line per event with the standard envelope.
"""

import json
import os
from datetime import datetime, timezone
from enum import auto
from pathlib import Path
from typing import Final
from uuid import uuid4

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.event_envelope import (
    EventEnvelope,
    EventId,
    EventSource,
    EventType,
    IsoTimestamp,
)
from loguru import logger
from pydantic import Field

ENV_CONVERGE_EVENT_SOURCE: Final[EventSource] = EventSource("env_converge")


class EnvConvergeEventType(UpperCaseStrEnum):
    """All event types env-converge may emit."""

    STATE_CAPTURED = auto()
    OVERLAY_APPLIED = auto()
    UNIT_RUN = auto()
    UNIT_FAILED = auto()
    PACKAGE_INSTALLED = auto()
    PACKAGE_UNAVAILABLE = auto()
    CONVERGE_COMPLETED = auto()
    UPGRADE_STARTED = auto()
    UPGRADE_COMPLETED = auto()


class EnvConvergeEvent(EventEnvelope):
    """Base envelope for env-converge events; payload rides in `detail`."""

    detail: dict[str, object] = Field(
        default_factory=dict, description="Event-specific payload"
    )


def default_events_path() -> Path | None:
    """Where the event stream lives, or None when MNGR_HOST_DIR is unset (bare dev runs)."""
    host_dir = os.environ.get("MNGR_HOST_DIR", "")
    if not host_dir:
        return None
    return (
        Path(host_dir)
        / "plugin"
        / "env-converge"
        / "events"
        / "env_converge"
        / "events.jsonl"
    )


def emit_event(event_type: EnvConvergeEventType, detail: dict[str, object]) -> None:
    """Append one event line; best-effort (environment work must not fail on logging)."""
    events_path = default_events_path()
    if events_path is None:
        logger.debug(
            "Skipping env-converge event {} (MNGR_HOST_DIR unset)", event_type.value
        )
        return
    event = EnvConvergeEvent(
        timestamp=IsoTimestamp(datetime.now(timezone.utc).isoformat()),
        type=EventType(event_type.value.lower()),
        event_id=EventId(f"evc-{uuid4().hex}"),
        source=ENV_CONVERGE_EVENT_SOURCE,
        detail=detail,
    )
    try:
        events_path.parent.mkdir(parents=True, exist_ok=True)
        with events_path.open("a") as f:
            f.write(json.dumps(event.to_jsonl_dict()) + "\n")
    except OSError as e:
        logger.warning("Cannot append env-converge event to {}: {}", events_path, e)

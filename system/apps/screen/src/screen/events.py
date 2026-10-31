"""The Screen's event stream, ``data/.state/screen/events/screen/events.jsonl``, one line per event with the
standard envelope (the style guide's "Event logging to disk")."""

import json
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Final
from uuid import uuid4

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.pure import pure

SCREEN_EVENT_SOURCE: Final[EventSource] = EventSource("screen")
EVENTS_RELATIVE_PATH: Final[Path] = Path("events") / "screen" / "events.jsonl"


class ScreenEventType(UpperCaseStrEnum):
    """The events the Screen records."""

    SCREEN_STOPPED = auto()


class ScreenEvent(EventEnvelope):
    """One Screen event; the payload rides in ``detail``."""

    detail: dict[str, object] = Field(default_factory=dict, description="Event-specific payload")


@pure
def build_screen_event(event_type: ScreenEventType, detail: dict[str, object], now: datetime) -> ScreenEvent:
    return ScreenEvent(
        timestamp=IsoTimestamp(now.astimezone(timezone.utc).isoformat()),
        type=EventType(event_type.value.lower()),
        event_id=EventId(f"scr-{uuid4().hex}"),
        source=SCREEN_EVENT_SOURCE,
        detail=detail,
    )


def append_screen_event(state_dir: Path, event_type: ScreenEventType, detail: dict[str, object]) -> None:
    """Append one event line; best-effort, since the stack's lifecycle must not fail on logging."""
    events_path = state_dir / EVENTS_RELATIVE_PATH
    event = build_screen_event(event_type, detail, datetime.now(timezone.utc))
    try:
        events_path.parent.mkdir(parents=True, exist_ok=True)
        with events_path.open("a") as events_file:
            events_file.write(json.dumps(event.model_dump()) + "\n")
    except OSError as e:
        logger.warning("Cannot append the Screen event to {}: {}", events_path, e)

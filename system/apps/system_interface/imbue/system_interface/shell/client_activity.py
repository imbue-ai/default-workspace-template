"""The append-only client-activity log: which client sent which message, and which desktop it switched to.

Lives at ``<state dir>/events/client_activity/events.jsonl`` with the ``message`` and
``desktop_switch`` shapes of desktop contracts.md section 6. An app posts a ``message`` whenever
a user sends one to one of its pages, and the shell records a ``desktop_switch`` on every client
report that names a different previous desktop, so an agent can work out which client (and
desktop) a request came from.
"""

import json
import threading
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.logging import generate_log_event_id
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

CLIENT_ACTIVITY_EVENT_SOURCE: Final[EventSource] = EventSource("client_activity")
MESSAGE_EVENT_TYPE: Final[EventType] = EventType("message")
DESKTOP_SWITCH_EVENT_TYPE: Final[EventType] = EventType("desktop_switch")

# Message text is truncated at write time: the log exists to say which client asked, not to
# duplicate the apps' own transcripts.
MESSAGE_TEXT_TRUNCATION_LIMIT: Final[int] = 500
# How many recent messages each client contributes to the ``context`` summary.
RECENT_MESSAGES_PER_CLIENT: Final[int] = 5


class ClientMessageEvent(EventEnvelope):
    """A message a client sent to an app's page."""

    client_id: str = Field(description="The sending client")
    desktop_id: str = Field(description="The desktop the client was on")
    app: str = Field(description="The app the message went to")
    key: str = Field(
        description="The marker of the page the message went to (a chat id); empty for a page without one"
    )
    text: str = Field(description="The message text, truncated at write time")
    is_text_truncated: bool = Field(description="Whether the text was cut to the limit")


class DesktopSwitchEvent(EventEnvelope):
    """A client changed its active desktop (desktop contracts.md section 6)."""

    client_id: str = Field(description="The switching client")
    from_desktop_id: str = Field(description="The desktop left ('' when unknown)")
    to_desktop_id: str = Field(description="The desktop entered")


def _now_iso() -> IsoTimestamp:
    return IsoTimestamp(format_nanosecond_iso_timestamp(datetime.now(timezone.utc)))


def _new_event_id() -> EventId:
    return EventId(generate_log_event_id())


@pure
def truncate_message_text(text: str) -> tuple[str, bool]:
    if len(text) <= MESSAGE_TEXT_TRUNCATION_LIMIT:
        return text, False
    return text[:MESSAGE_TEXT_TRUNCATION_LIMIT], True


class ClientActivityLog(MutableModel):
    """Appends to and reads the client-activity event file."""

    events_path: Path = Field(frozen=True, description="The events.jsonl file")
    _append_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def _append(self, event: EventEnvelope) -> None:
        path = self.events_path
        with self._append_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as event_file:
                event_file.write(event.model_dump_json() + "\n")

    def append_message(self, client_id: str, desktop_id: str, app: str, key: str, text: str) -> None:
        truncated, is_truncated = truncate_message_text(text)
        self._append(
            ClientMessageEvent(
                timestamp=_now_iso(),
                type=MESSAGE_EVENT_TYPE,
                event_id=_new_event_id(),
                source=CLIENT_ACTIVITY_EVENT_SOURCE,
                client_id=client_id,
                desktop_id=desktop_id,
                app=app,
                key=key,
                text=truncated,
                is_text_truncated=is_truncated,
            )
        )

    def append_desktop_switch(self, client_id: str, from_desktop_id: str, to_desktop_id: str) -> None:
        self._append(
            DesktopSwitchEvent(
                timestamp=_now_iso(),
                type=DESKTOP_SWITCH_EVENT_TYPE,
                event_id=_new_event_id(),
                source=CLIENT_ACTIVITY_EVENT_SOURCE,
                client_id=client_id,
                from_desktop_id=from_desktop_id,
                to_desktop_id=to_desktop_id,
            )
        )

    def read_events(self) -> list[dict[str, Any]]:
        """Every parseable event line, in file (chronological) order."""
        path = self.events_path
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as e:
                logger.opt(exception=e).warning("Skipped an unparsable client-activity event line")
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
        return events


@pure
def _event_desktop_id(event: dict[str, Any]) -> str | None:
    event_type = event.get("type")
    if event_type == DESKTOP_SWITCH_EVENT_TYPE:
        return str(event.get("to_desktop_id", "")) or None
    if event_type == MESSAGE_EVENT_TYPE:
        return str(event.get("desktop_id", "")) or None
    return None


@pure
def _empty_client_summary(client_id: str) -> dict[str, Any]:
    return {
        "client_id": client_id,
        "active_desktop": None,
        "last_seen": "",
        "is_connected": False,
        "recent_messages": [],
    }


@pure
def summarize_client_activity(
    events: Sequence[dict[str, Any]],
    # The broadcaster's registrations: each carries ``client_id`` and ``active_desktop`` ("" when the client
    # has not reported one).
    connected_clients: Sequence[Mapping[str, str]],
) -> list[dict[str, Any]]:
    """Fold the log into one summary per client, most recently seen first (the ``context`` op).

    Every connected client is listed with its live desktop, whether or not the log holds anything
    for it: a client that has neither messaged nor switched desktops yet has no event, and is
    still the one an agent's op should land on.
    """
    summary_by_client_id: dict[str, dict[str, Any]] = {}
    for event in events:
        client_id = str(event.get("client_id", ""))
        if not client_id:
            continue
        summary = summary_by_client_id.setdefault(client_id, _empty_client_summary(client_id))
        summary["last_seen"] = str(event.get("timestamp", ""))
        desktop_id = _event_desktop_id(event)
        if desktop_id is not None:
            summary["active_desktop"] = desktop_id
        if event.get("type") == MESSAGE_EVENT_TYPE:
            summary["recent_messages"].append(
                {
                    "timestamp": str(event.get("timestamp", "")),
                    "app": str(event.get("app", "")),
                    "key": str(event.get("key", "")),
                    "text": str(event.get("text", "")),
                }
            )
            del summary["recent_messages"][:-RECENT_MESSAGES_PER_CLIENT]
    # The live registrations are fresher than the log (and the only record of a client that
    # has logged nothing yet), so they settle the desktop.
    for connected in connected_clients:
        client_id = connected["client_id"]
        summary = summary_by_client_id.setdefault(client_id, _empty_client_summary(client_id))
        summary["is_connected"] = True
        summary["active_desktop"] = connected["active_desktop"] or None
    return sorted(summary_by_client_id.values(), key=lambda summary: summary["last_seen"], reverse=True)


@pure
def find_client_id_for_page(events: Sequence[dict[str, Any]], app: str, marker: str) -> str | None:
    """The client that most recently messaged one page (by app and marker), or None: how an agent-initiated op finds its requester."""
    if not marker:
        return None
    for event in reversed(events):
        if event.get("type") == MESSAGE_EVENT_TYPE and event.get("app") == app and event.get("key") == marker:
            client_id = str(event.get("client_id", ""))
            return client_id or None
    return None

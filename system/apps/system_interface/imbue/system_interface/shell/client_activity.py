"""The append-only client-activity log: which client sent which message, and which view it switched to.

Lives at ``<state dir>/events/client_activity/events.jsonl`` with the ``message`` and
``view_switch`` shapes of contracts.md section 5. The chat app posts a ``message`` on every
send and the shell records a ``view_switch`` on every client report that names a different
previous view, so an agent can work out which client (and view) a request came from.
"""

import json
import threading
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
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
from imbue.system_interface.shell.primitives import ADDRESS_INSTANCE_PARAMETER
from imbue.system_interface.shell.primitives import ADDRESS_SCHEME

CLIENT_ACTIVITY_EVENT_SOURCE: Final[EventSource] = EventSource("client_activity")
MESSAGE_EVENT_TYPE: Final[EventType] = EventType("message")
VIEW_SWITCH_EVENT_TYPE: Final[EventType] = EventType("view_switch")

# Message text is truncated at write time: the log exists to say which client asked, not to
# duplicate the apps' own transcripts.
MESSAGE_TEXT_TRUNCATION_LIMIT: Final[int] = 500
# How many recent messages each client contributes to the ``context`` summary.
RECENT_MESSAGES_PER_CLIENT: Final[int] = 5


class ClientMessageEvent(EventEnvelope):
    """A message a client sent to an app's instance."""

    client_id: str = Field(description="The sending client")
    device_kind: str = Field(description="'desktop' or 'mobile'")
    view_id: str = Field(description="The view the client was on")
    app: str = Field(description="The app the message went to")
    key: str = Field(description="The instance key the message went to")
    text: str = Field(description="The message text, truncated at write time")
    is_text_truncated: bool = Field(description="Whether the text was cut to the limit")


class ViewSwitchEvent(EventEnvelope):
    """A client changed its active view."""

    client_id: str = Field(description="The switching client")
    device_kind: str = Field(description="'desktop' or 'mobile'")
    from_view_id: str = Field(description="The view left ('' when unknown)")
    to_view_id: str = Field(description="The view entered")


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

    def append_message(self, client_id: str, device_kind: str, view_id: str, app: str, key: str, text: str) -> None:
        truncated, is_truncated = truncate_message_text(text)
        self._append(
            ClientMessageEvent(
                timestamp=_now_iso(),
                type=MESSAGE_EVENT_TYPE,
                event_id=_new_event_id(),
                source=CLIENT_ACTIVITY_EVENT_SOURCE,
                client_id=client_id,
                device_kind=device_kind,
                view_id=view_id,
                app=app,
                key=key,
                text=truncated,
                is_text_truncated=is_truncated,
            )
        )

    def append_view_switch(self, client_id: str, device_kind: str, from_view_id: str, to_view_id: str) -> None:
        self._append(
            ViewSwitchEvent(
                timestamp=_now_iso(),
                type=VIEW_SWITCH_EVENT_TYPE,
                event_id=_new_event_id(),
                source=CLIENT_ACTIVITY_EVENT_SOURCE,
                client_id=client_id,
                device_kind=device_kind,
                from_view_id=from_view_id,
                to_view_id=to_view_id,
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
def _message_address(app: str, key: str) -> str:
    """The address a message went to, spelled as the event recorded it (the log is not validated against the registry)."""
    if key == "":
        return f"{ADDRESS_SCHEME}{app}"
    return f"{ADDRESS_SCHEME}{app}?{ADDRESS_INSTANCE_PARAMETER}{key}"


@pure
def _event_view_id(event: dict[str, Any]) -> str | None:
    event_type = event.get("type")
    if event_type == VIEW_SWITCH_EVENT_TYPE:
        return str(event.get("to_view_id", "")) or None
    if event_type == MESSAGE_EVENT_TYPE:
        return str(event.get("view_id", "")) or None
    return None


@pure
def summarize_client_activity(
    events: Sequence[dict[str, Any]],
    connected_client_ids: AbstractSet[str],
) -> list[dict[str, Any]]:
    """Fold the log into one summary per client, most recently seen first (the ``context`` op)."""
    summary_by_client_id: dict[str, dict[str, Any]] = {}
    for event in events:
        client_id = str(event.get("client_id", ""))
        if not client_id:
            continue
        summary = summary_by_client_id.setdefault(
            client_id,
            {
                "client_id": client_id,
                "device_kind": "",
                "active_view": None,
                "last_seen": "",
                "is_connected": False,
                "recent_messages": [],
            },
        )
        summary["last_seen"] = str(event.get("timestamp", ""))
        device_kind = str(event.get("device_kind", ""))
        if device_kind:
            summary["device_kind"] = device_kind
        view_id = _event_view_id(event)
        if view_id is not None:
            summary["active_view"] = view_id
        if event.get("type") == MESSAGE_EVENT_TYPE:
            summary["recent_messages"].append(
                {
                    "timestamp": str(event.get("timestamp", "")),
                    "address": _message_address(str(event.get("app", "")), str(event.get("key", ""))),
                    "text": str(event.get("text", "")),
                }
            )
            del summary["recent_messages"][:-RECENT_MESSAGES_PER_CLIENT]
    for client_id in connected_client_ids:
        if client_id in summary_by_client_id:
            summary_by_client_id[client_id]["is_connected"] = True
    return sorted(summary_by_client_id.values(), key=lambda summary: summary["last_seen"], reverse=True)


@pure
def find_client_id_for_instance(events: Sequence[dict[str, Any]], app: str, key: str) -> str | None:
    """The client that most recently messaged one instance, or None: how an agent-initiated op finds its requester."""
    if not key:
        return None
    for event in reversed(events):
        if event.get("type") == MESSAGE_EVENT_TYPE and event.get("app") == app and event.get("key") == key:
            client_id = str(event.get("client_id", ""))
            return client_id or None
    return None

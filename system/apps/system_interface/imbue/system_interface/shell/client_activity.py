"""The append-only client-activity log: which client sent which message, which desktop it switched to, which
windows were opened, and which windows a person interacted with.

Lives at ``<state dir>/events/client_activity/events.jsonl`` with the ``message``, ``desktop_switch``,
``window_opened`` and ``window_input`` shapes of desktop contracts.md section 6. An app posts a ``message`` whenever a
user sends one to one of its pages, the shell records a ``desktop_switch`` on every client report that names a
different previous desktop, so an agent can work out which client (and desktop) a request came from, it records a
``window_opened`` on every open (an app, never a path or a title), and it records a ``window_input`` once per
client, window and minute in which the window's page reported keyboard or pointer input (or, for a page that reports
none, focus landing in it), which is how use of an app shows in the log.

The log is bounded the way the announcement stream is: past its threshold it is staged aside before a write and
gzipped on a thread of its own, the newest archives kept; the readers read the live stream alone.
"""

import json
import threading
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final

from event_streams.archive import compress_staged_streams
from event_streams.archive import stage_oversized_stream
from event_streams.archive import staged_streams
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError
from workspace_layout.answers import ClientActivitySummary
from workspace_layout.ops import ClientActivityReport
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import DesktopId

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.logging import generate_log_event_id
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.ws_broadcaster import ConnectionRegistration

CLIENT_ACTIVITY_EVENT_SOURCE: Final[EventSource] = EventSource("client_activity")
MESSAGE_EVENT_TYPE: Final[EventType] = EventType("message")
DESKTOP_SWITCH_EVENT_TYPE: Final[EventType] = EventType("desktop_switch")
WINDOW_OPENED_EVENT_TYPE: Final[EventType] = EventType("window_opened")
WINDOW_INPUT_EVENT_TYPE: Final[EventType] = EventType("window_input")

# Past this size the stream is staged aside before a write and gzipped off-thread, this many archives kept.
CLIENT_ACTIVITY_ARCHIVE_THRESHOLD_BYTES: Final[int] = 8 * 1024 * 1024
KEPT_CLIENT_ACTIVITY_ARCHIVE_COUNT: Final[int] = 2
# A window's last input minute is remembered this long for the once-per-minute rule, then forgotten.
_WINDOW_INPUT_MEMORY_MINUTES: Final[int] = 2

# Message text is truncated at write time: the log exists to say which client asked, not to
# duplicate the apps' own transcripts.
MESSAGE_TEXT_TRUNCATION_LIMIT: Final[int] = 500
# How many recent messages each client contributes to the ``context`` summary.
RECENT_MESSAGES_PER_CLIENT: Final[int] = 5


class ClientMessageEvent(EventEnvelope):
    """A message a client sent to an app's page."""

    client_id: ClientId = Field(description="The sending client")
    desktop_id: DesktopId = Field(description="The desktop the client was on")
    app: str = Field(description="The app the message went to")
    key: str = Field(
        description="The marker of the page the message went to (a chat id); empty for a page without one"
    )
    text: str = Field(description="The message text, truncated at write time")
    is_text_truncated: bool = Field(description="Whether the text was cut to the limit")


class WindowOpenedEvent(EventEnvelope):
    """A window of an app was opened, or an existing one answered to an open (desktop contracts.md section 6)."""

    client_id: str = Field(description="The requesting client; empty for an open with no client to place it for")
    desktop_id: str = Field(description="The desktop the window is on")
    app: str = Field(description="The app whose page the window shows; never the path or the title")
    window_id: str = Field(description="The window")
    is_new: bool = Field(description="Whether the open minted the window, rather than answering an existing one")
    requester: str = Field(
        description="The op requester's spelling (an app, or app:marker) when an op opened it; empty for a client's own"
    )


class WindowInputKind(LowerCaseStrEnum):
    """What a ``window_input`` records: the page's own input report, or focus landing in a page that reports none."""

    INPUT = auto()
    FOCUS = auto()


class WindowInputEvent(EventEnvelope):
    """A person interacted with a window's page in this minute (desktop contracts.md section 6): which app and
    when, never what was typed or clicked."""

    client_id: str = Field(description="The client whose page reported")
    desktop_id: str = Field(description="The window's desktop, else the client's active one, else empty")
    app: str = Field(description="The app whose page the window shows; never the path or the title")
    window_id: str = Field(description="The window")
    kind: WindowInputKind = Field(description="The page's own report, or focus landing in a page that reports none")


class DesktopSwitchEvent(EventEnvelope):
    """A client changed its active desktop (desktop contracts.md section 6)."""

    client_id: str = Field(description="The switching client")
    from_desktop_id: str = Field(description="The desktop left ('' when unknown)")
    to_desktop_id: str = Field(description="The desktop entered")


def _now_iso() -> IsoTimestamp:
    return _iso(datetime.now(timezone.utc))


def _iso(moment: datetime) -> IsoTimestamp:
    return IsoTimestamp(format_nanosecond_iso_timestamp(moment))


@pure
def _minute_of(moment: datetime) -> int:
    """The UTC minute ``moment`` falls in, as whole minutes since the epoch."""
    return int(moment.timestamp()) // 60


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
    archive_threshold_bytes: int = Field(
        default=CLIENT_ACTIVITY_ARCHIVE_THRESHOLD_BYTES,
        frozen=True,
        description="The size past which the stream is staged aside before a write",
    )
    _append_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _archive_thread: threading.Thread | None = PrivateAttr(default=None)
    _last_input_minute_by_client_window: dict[tuple[str, str], int] = PrivateAttr(default_factory=dict)

    def _append(self, event: EventEnvelope) -> None:
        with self._append_lock:
            self._append_locked(event)

    def _append_locked(self, event: EventEnvelope) -> None:
        """Append one event; the caller holds the append lock. A stream past the threshold is staged aside first,
        and whatever is staged (this write's, or a stopped shell's) is compressed on a thread of its own. Appends
        come from request threads, so one archive thread runs at a time: a stream staged while it runs is found by
        the first append after it finishes."""
        path = self.events_path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if stage_oversized_stream(path, self.archive_threshold_bytes):
                logger.info("Moved the oversized client-activity log {} aside for archiving", path)
        except OSError as e:
            logger.opt(exception=e).warning("Failed to check the client-activity log {} for archiving", path)
        with path.open("a", encoding="utf-8") as event_file:
            event_file.write(event.model_dump_json() + "\n")
        is_archiving = self._archive_thread is not None and self._archive_thread.is_alive()
        if not is_archiving and staged_streams(path):
            self._archive_thread = threading.Thread(
                target=self._compress_staged_streams, daemon=True, name="client-activity-archive"
            )
            self._archive_thread.start()

    def _compress_staged_streams(self) -> None:
        try:
            compress_staged_streams(self.events_path, KEPT_CLIENT_ACTIVITY_ARCHIVE_COUNT)
        except OSError as e:
            logger.opt(exception=e).warning(
                "Failed to archive the staged client-activity log beside {}", self.events_path
            )

    def append_message(self, report: ClientActivityReport) -> None:
        truncated, is_truncated = truncate_message_text(report.text)
        self._append(
            ClientMessageEvent(
                timestamp=_now_iso(),
                type=MESSAGE_EVENT_TYPE,
                event_id=_new_event_id(),
                source=CLIENT_ACTIVITY_EVENT_SOURCE,
                client_id=report.client_id,
                desktop_id=report.desktop_id,
                app=report.app,
                key=report.key,
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

    def append_window_opened(
        self, client_id: str, desktop_id: str, app: str, window_id: str, is_new: bool, requester: str
    ) -> None:
        self._append(
            WindowOpenedEvent(
                timestamp=_now_iso(),
                type=WINDOW_OPENED_EVENT_TYPE,
                event_id=_new_event_id(),
                source=CLIENT_ACTIVITY_EVENT_SOURCE,
                client_id=client_id,
                desktop_id=desktop_id,
                app=app,
                window_id=window_id,
                is_new=is_new,
                requester=requester,
            )
        )

    def append_window_input(
        self, client_id: str, desktop_id: str, app: str, window_id: str, kind: WindowInputKind, now: datetime
    ) -> bool:
        """Record input in a window once per client, window and minute of ``now``; answers whether a line was
        written (a second report of the window inside the minute writes nothing, whatever its kind)."""
        minute = _minute_of(now)
        key = (client_id, window_id)
        with self._append_lock:
            remembered = self._last_input_minute_by_client_window
            if remembered.get(key) == minute:
                return False
            for stale_key in [
                k for k, last_minute in remembered.items() if last_minute <= minute - _WINDOW_INPUT_MEMORY_MINUTES
            ]:
                del remembered[stale_key]
            remembered[key] = minute
            self._append_locked(
                WindowInputEvent(
                    timestamp=_iso(now),
                    type=WINDOW_INPUT_EVENT_TYPE,
                    event_id=_new_event_id(),
                    source=CLIENT_ACTIVITY_EVENT_SOURCE,
                    client_id=client_id,
                    desktop_id=desktop_id,
                    app=app,
                    window_id=window_id,
                    kind=kind,
                )
            )
        return True

    def read_events(self) -> list[dict[str, Any]]:
        """Every parseable event line of the live stream, in file (chronological) order; an archived stream is
        not read."""
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
    connected_clients: Sequence[ConnectionRegistration],
) -> list[ClientActivitySummary]:
    """Fold the log into one summary per client, most recently seen first (the ``context`` op).

    Every connected client is listed with its live desktop, whether or not the log holds anything
    for it: a client that has neither messaged nor switched desktops yet has no event, and is
    still the one an agent's op should land on. A pop-out's registration makes its client connected
    but names no desktop, so the client's desktop stays its main window's (or the log's). A client
    whose logged id or desktop is off its rule is left out, with a warning, rather than failing the op.
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
        summary = summary_by_client_id.setdefault(connected.client_id, _empty_client_summary(connected.client_id))
        summary["is_connected"] = True
        if not connected.is_pop_out:
            summary["active_desktop"] = connected.active_desktop or None
    summaries: list[ClientActivitySummary] = []
    for summary in sorted(summary_by_client_id.values(), key=lambda summary: summary["last_seen"], reverse=True):
        try:
            summaries.append(ClientActivitySummary.model_validate(summary))
        except ValidationError as e:
            logger.warning("Left client {!r} out of the activity summary: {}", summary["client_id"], e)
    return summaries


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

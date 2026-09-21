"""The avatar's mood (pinned-taskbar-entries plan section 4.6): whether any agent on the machine is working, read
from the agents event file the mngr observer writes, with plain JSON parsing and no mngr import.

The file is append-only JSONL under the standard event envelope: a full snapshot of every agent every five
minutes, one agent's state whenever its host shows activity, and a removal when an agent is destroyed. The fold
reads from the end of the file back to the most recent snapshot, applies every later state and removal on top,
drops the workspace's own services agent (labelled primary), and asks whether any remaining agent is running.
Every convention relied on is a constant below; a change to one shows up as a stale or idle avatar, never as an
error. Past ten minutes (twice the snapshot interval) the status is stale; a missing file is stale from the start.
"""

import json
import os
import re
import threading
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from watchdog.events import FileMovedEvent
from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer as _Observer

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.avatar.designs import AvatarMood
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

# mngr's conventions, duplicated rather than imported: the host directory's environment variable and fallback,
# the observer's event file under it, the three event types the fold reads, the label that marks the workspace's
# services agent, and the two lifecycle states that mean an agent is working.
ENV_MNGR_HOST_DIR: Final[str] = "MNGR_HOST_DIR"
DEFAULT_MNGR_HOST_DIRNAME: Final[str] = ".mngr"
AGENT_EVENTS_RELATIVE_PATH: Final[Path] = Path("events") / "mngr" / "agents" / "events.jsonl"
FULL_STATE_EVENT_TYPE: Final[str] = "AGENTS_FULL_STATE"
AGENT_STATE_EVENT_TYPE: Final[str] = "AGENT_STATE"
AGENT_REMOVED_EVENT_TYPE: Final[str] = "AGENT_REMOVED"
PRIMARY_LABEL_KEY: Final[str] = "is_primary"
WORKING_AGENT_STATES: Final[frozenset[str]] = frozenset({"RUNNING", "RUNNING_UNKNOWN_AGENT_TYPE"})
# Twice the observer's snapshot interval.
STALE_AFTER: Final[timedelta] = timedelta(minutes=10)

# How much of the file's tail is read per step while looking back for the last snapshot.
_TAIL_BLOCK_BYTES: Final[int] = 64 * 1024
# How long a burst of writes is allowed to settle before one refold.
DEBOUNCE_SECONDS: Final[float] = 0.5
# How often staleness is re-checked when nothing is written (it changes with the clock alone).
STALE_CHECK_INTERVAL_SECONDS: Final[float] = 30.0

# The envelope's timestamp: ISO 8601 UTC with up to nine fractional digits, of which Python reads six.
_TIMESTAMP_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$"
)


class AvatarStatus(FrozenModel):
    """What the avatar wears: the mood, and whether the file it came from may be out of date."""

    mood: AvatarMood = Field(description="Working when any agent but the services agent is running")
    is_stale: bool = Field(description="Whether the newest event is older than the stale threshold, or absent")


@pure
def avatar_status_wire_json(status: AvatarStatus) -> dict[str, Any]:
    """The ``avatar_status`` message's payload (pinned-taskbar-entries plan section 5.3)."""
    return {"mood": status.mood.value, "is_stale": status.is_stale}


STALE_IDLE_STATUS: Final[AvatarStatus] = AvatarStatus(mood=AvatarMood.IDLE, is_stale=True)


@pure
def agent_events_path(environ: Mapping[str, str]) -> Path:
    """Where the observer's agents event file is: under ``MNGR_HOST_DIR``, else under ``~/.mngr``."""
    host_dir = environ.get(ENV_MNGR_HOST_DIR, "")
    base = Path(host_dir) if host_dir else Path.home() / DEFAULT_MNGR_HOST_DIRNAME
    return base / AGENT_EVENTS_RELATIVE_PATH


@pure
def parse_event_timestamp(raw: str) -> datetime | None:
    """The envelope's timestamp as an aware datetime, or None for one that does not parse."""
    match = _TIMESTAMP_PATTERN.match(raw.strip())
    if match is None:
        return None
    seconds, fraction, zone = match.groups()
    microseconds = (fraction or "").ljust(6, "0")[:6]
    text = f"{seconds}.{microseconds}{'+00:00' if zone == 'Z' else zone}"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


@pure
def _is_primary(agent: Mapping[str, Any]) -> bool:
    labels = agent.get("labels")
    if not isinstance(labels, Mapping):
        return False
    value = labels.get(PRIMARY_LABEL_KEY)
    return value is True or (isinstance(value, str) and value.lower() == "true")


@pure
def _agent_id_and_state(agent: Any) -> tuple[str, str] | None:
    """An agent's id and lifecycle state from its details, or None for details without them."""
    if not isinstance(agent, Mapping) or not isinstance(agent.get("id"), str):
        return None
    state = agent.get("state")
    return (agent["id"], state if isinstance(state, str) else "")


def _take_agent(state_by_id: dict[str, str], primary_ids: set[str], agent: Any) -> None:
    """Record one agent's state in the fold, and whether it is the primary one; details without an id are skipped."""
    identified = _agent_id_and_state(agent)
    if identified is None:
        return
    agent_id, state = identified
    state_by_id[agent_id] = state
    if _is_primary(agent):
        primary_ids.add(agent_id)
    else:
        primary_ids.discard(agent_id)


@pure
def _parsed_events(lines: Sequence[str]) -> list[dict[str, Any]]:
    """Every line that is a JSON object; a line that is not is skipped with a warning."""
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError as e:
            logger.warning("Skipped an unparsable agents event line {}: {}", line_number, e)
            continue
        if not isinstance(parsed, dict):
            logger.warning("Skipped an agents event line {} that is not an object", line_number)
            continue
        events.append(parsed)
    return events


@pure
def fold_agent_events(lines: Sequence[str], now: datetime) -> AvatarStatus:
    """The status the event lines fold to: the last full snapshot with every later state and removal applied, the
    primary agent dropped, working when any remaining agent runs; stale past the threshold or with no timestamp."""
    events = _parsed_events(lines)
    snapshot_index = next(
        (index for index in range(len(events) - 1, -1, -1) if events[index].get("type") == FULL_STATE_EVENT_TYPE), None
    )
    state_by_id: dict[str, str] = {}
    primary_ids: set[str] = set()
    for event in events[snapshot_index if snapshot_index is not None else 0 :]:
        event_type = event.get("type")
        if event_type == FULL_STATE_EVENT_TYPE:
            state_by_id.clear()
            primary_ids.clear()
            agents = event.get("agents")
            for agent in agents if isinstance(agents, list) else []:
                _take_agent(state_by_id, primary_ids, agent)
        elif event_type == AGENT_STATE_EVENT_TYPE:
            _take_agent(state_by_id, primary_ids, event.get("agent"))
        elif event_type == AGENT_REMOVED_EVENT_TYPE:
            agent_id = event.get("agent_id")
            if isinstance(agent_id, str):
                state_by_id.pop(agent_id, None)
                primary_ids.discard(agent_id)
        else:
            pass
    is_working = any(
        state in WORKING_AGENT_STATES for agent_id, state in state_by_id.items() if agent_id not in primary_ids
    )
    newest = next(
        (
            parsed
            for event in reversed(events)
            if isinstance(event.get("timestamp"), str) and (parsed := parse_event_timestamp(event["timestamp"]))
        ),
        None,
    )
    is_stale = newest is None or now.astimezone(timezone.utc) - newest.astimezone(timezone.utc) > STALE_AFTER
    return AvatarStatus(mood=AvatarMood.WORKING if is_working else AvatarMood.IDLE, is_stale=is_stale)


def read_tail_lines_back_to_snapshot(path: Path) -> list[str]:
    """The file's lines from the last full snapshot on (the whole file when it holds none), read from the end in
    blocks so a long-lived file is not read whole; an absent file is no lines."""
    if not path.is_file():
        return []
    marker = f'"{FULL_STATE_EVENT_TYPE}"'.encode()
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        position = stream.tell()
        chunks: list[bytes] = []
        is_whole = position == 0
        # Whether a newer block's cut first line held the marker: that line starts in an older block, and the read
        # ends at the first block that holds its start (a marker split across two blocks reads one block further).
        is_marker_in_cut_line = False
        while position > 0:
            step = min(_TAIL_BLOCK_BYTES, position)
            position -= step
            stream.seek(position)
            chunk = stream.read(step)
            chunks.insert(0, chunk)
            is_whole = position == 0
            first_newline = chunk.find(b"\n")
            if first_newline == -1:
                is_marker_in_cut_line = is_marker_in_cut_line or marker in chunk
                continue
            if is_marker_in_cut_line or marker in chunk[first_newline + 1 :]:
                break
            is_marker_in_cut_line = marker in chunk[:first_newline]
    text = b"".join(chunks).decode("utf-8", errors="replace")
    lines = text.split("\n")
    # A read that stopped mid-file starts in the middle of a line, which is not this fold's to read.
    return lines if is_whole else lines[1:]


def read_avatar_status(path: Path, now: datetime) -> AvatarStatus:
    return fold_agent_events(read_tail_lines_back_to_snapshot(path), now)


class _EventsFileHandler(FileSystemEventHandler):
    """Fires ``on_change`` on mutating events whose path is the events file."""

    basename: str
    on_change: Callable[[], None]

    def _maybe_fire(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        paths = [event.src_path]
        if isinstance(event, FileMovedEvent):
            paths.append(event.dest_path)
        if any(os.path.basename(str(path)) == self.basename for path in paths):
            self.on_change()

    on_modified = _maybe_fire
    on_created = _maybe_fire
    on_moved = _maybe_fire
    on_closed = _maybe_fire


def _make_events_file_handler(basename: str, on_change: Callable[[], None]) -> _EventsFileHandler:
    handler = _EventsFileHandler()
    handler.basename = basename
    handler.on_change = on_change
    return handler


class AvatarStatusReader(MutableModel):
    """Keeps the avatar's status current: watches the events file, refolds after a settled burst of writes and on
    a timer for staleness, and pushes ``avatar_status`` to every window when the mood or the staleness changes."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    events_path: Path = Field(frozen=True, description="The observer's agents event file")
    broadcaster: WebSocketBroadcaster = Field(frozen=True, description="Where ``avatar_status`` goes")
    debounce_seconds: float = Field(default=DEBOUNCE_SECONDS, frozen=True, description="How long a burst settles")
    stale_check_interval_seconds: float = Field(
        default=STALE_CHECK_INTERVAL_SECONDS, frozen=True, description="How often staleness is re-checked"
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _status: AvatarStatus = PrivateAttr(default=STALE_IDLE_STATUS)
    _observer: Any | None = PrivateAttr(default=None)
    _stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _wake: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: threading.Thread | None = PrivateAttr(default=None)

    def current(self) -> AvatarStatus:
        with self._lock:
            return self._status

    def start(self) -> None:
        """Fold once now, then watch the file and re-check on the interval."""
        self.refresh()
        self._ensure_watching()
        thread = threading.Thread(target=self._run, daemon=True, name="avatar-status")
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def refresh(self) -> None:
        """Refold the file; a change of mood or staleness is pushed to every window."""
        try:
            status = read_avatar_status(self.events_path, datetime.now(timezone.utc))
        except OSError as e:
            logger.opt(exception=e).warning("Could not read the agents event file at {}", self.events_path)
            status = STALE_IDLE_STATUS
        with self._lock:
            is_changed = status != self._status
            self._status = status
        if is_changed:
            self.broadcaster.broadcast_avatar_status(avatar_status_wire_json(status))

    def _ensure_watching(self) -> None:
        """Watch the events directory once it exists; the shell creates nothing under an mngr directory."""
        if self._observer is not None or not self.events_path.parent.is_dir():
            return
        observer = _Observer()
        observer.schedule(
            _make_events_file_handler(self.events_path.name, self._wake.set), str(self.events_path.parent)
        )
        observer.daemon = True
        try:
            observer.start()
        except OSError as e:
            logger.opt(exception=e).error("Failed to watch the agents event file at {}", self.events_path)
            return
        self._observer = observer

    def _run(self) -> None:
        while not self._stop.is_set():
            is_woken = self._wake.wait(timeout=self.stale_check_interval_seconds)
            self._wake.clear()
            if self._stop.is_set():
                return
            if is_woken:
                self._stop.wait(timeout=self.debounce_seconds)
            self._ensure_watching()
            self.refresh()

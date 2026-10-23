"""Read a grok agent's session logs into UI events, and tail the live ones.

Built on the shared :mod:`~imbue.chat.harnesses.transcript_store` scaffolding. The top-level
sessions mngr_grok records for the agent (``list_root_session_dirs``) share one lane, merged by
record time, because ``/new`` and ``--resume`` interleave them; each subagent's child session,
named by its parent's ``subagent_spawned`` record and found with ``get_session_dir``, is a lane
of its own, kept out of the main stream. :class:`GrokTranscriptLoader` is the read half, which an
archived chat segment is read through; :class:`GrokSessionWatcher` adds the live half and reports
the background work the transcript says has finished.
"""

import heapq
import json
from operator import itemgetter
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.events import SPECIAL_EVENT_TYPE
from imbue.chat.harnesses.events import SpecialEventKind
from imbue.chat.harnesses.grok.session_parser import GrokLaneState
from imbue.chat.harnesses.grok.session_parser import SUBAGENT_SPAWNED_MARKER_TYPE
from imbue.chat.harnesses.grok.session_parser import TASK_FINISHED_MARKER_TYPE
from imbue.chat.harnesses.grok.session_parser import THINKING_SOURCE_MARKER_TYPE
from imbue.chat.harnesses.grok.session_parser import detail_for
from imbue.chat.harnesses.grok.session_parser import parse_record
from imbue.chat.harnesses.grok.session_parser import record_timestamp
from imbue.chat.harnesses.session_watcher import FinishedTasksCallback
from imbue.chat.harnesses.session_watcher import OnEventsCallback
from imbue.chat.harnesses.transcript_store import EventSource
from imbue.chat.harnesses.transcript_store import StoreBackedTranscriptLoader
from imbue.chat.harnesses.transcript_store import StoreBackedWatcher
from imbue.chat.harnesses.transcript_store import iter_line_spans
from imbue.chat.harnesses.transcript_store import split_at_last_complete_line
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr_grok.api import get_grok_home
from imbue.mngr_grok.api import get_session_dir
from imbue.mngr_grok.api import list_root_session_dirs

# The lane every top-level session's events share.
MAIN_LANE: Final[str] = "main"
UPDATES_FILENAME: Final[str] = "updates.jsonl"
# Grok's directory of session logs under its home.
SESSIONS_DIRNAME: Final[str] = "sessions"

# One complete line of a session log: (record time, byte offset, byte length, record).
_LogRecord = tuple[str, int, int, dict[str, Any]]


class _GrokSessionTail(MutableModel):
    """How far one grok session's log has been read, and the parser's state for it."""

    updates_path: Path = Field(description="The session's updates.jsonl")
    lane: GrokLaneState = Field(description="The parser's state for the session")
    byte_offset: int = Field(default=0, description="Bytes of the log consumed, through its last complete line")
    pending_thinking_source: EventSource | None = Field(
        default=None, description="The latest thought line, until the next event stamped has_thinking takes it"
    )

    @property
    def session_id(self) -> str:
        return self.lane.session_id

    @property
    def is_root(self) -> bool:
        return self.lane.is_root


# A log record with the session it came from, for merging top-level sessions by record time.
_SessionLogRecord = tuple[str, int, int, dict[str, Any], _GrokSessionTail]


def _decode_record(line: str | None) -> dict[str, Any] | None:
    """The JSON object a session-log line holds, or None."""
    if line is None or not line.strip():
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError as e:
        logger.warning("Skipping an undecodable grok session-log line: {}", e)
        return None
    return record if isinstance(record, dict) else None


class GrokTranscriptLoader(StoreBackedTranscriptLoader):
    """Reads a grok agent's top-level and child session logs into the store."""

    _agent_state_dir: Path
    _tail_by_session: dict[str, _GrokSessionTail]
    _root_session_ids: list[str]
    # Child sessions a subagent_spawned record named whose directory has not been found yet.
    _pending_child_session_ids: set[str]
    _subagent_metadata_by_session: dict[str, dict[str, str]]

    @classmethod
    def build_loader(cls, agent_info: AgentInfo) -> "GrokTranscriptLoader":
        loader = cls.__new__(cls)
        loader._init_loader(agent_info.id)
        loader._init_grok_state(agent_info.agent_state_dir)
        return loader

    def _init_grok_state(self, agent_state_dir: Path) -> None:
        self._agent_state_dir = agent_state_dir
        self._tail_by_session = {}
        self._root_session_ids = []
        self._pending_child_session_ids = set()
        self._subagent_metadata_by_session = {}
        self._store.ensure_lane(MAIN_LANE)

    def _on_task_finished_locked(self, session_id: str, task_id: str, will_wake: bool) -> None:
        """A top-level session's background task ended; only a live watcher has anyone to tell."""

    def _on_root_turn_started_locked(self, session_id: str) -> None:
        """A top-level session's turn started; only a live watcher has anyone to tell."""

    # base hooks

    def _refresh_locked(self) -> None:
        self._discover_root_sessions_locked()
        self._consume_root_sessions_locked()
        consumed_child_ids: set[str] = set()
        unconsumed = self._resolve_child_sessions_locked(consumed_child_ids)
        while unconsumed:
            for tail in unconsumed:
                for _timestamp, offset, length, record in self._read_appended_records_locked(tail):
                    self._parse_record_locked(tail, offset, length, record)
                consumed_child_ids.add(tail.session_id)
            unconsumed = self._resolve_child_sessions_locked(consumed_child_ids)

    def _selected_lane_ids_locked(self, session_id: str | None) -> list[str]:
        if session_id is None:
            return [MAIN_LANE]
        tail = self._tail_by_session.get(session_id)
        return [session_id] if tail is not None and not tail.is_root else []

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        with self._lock:
            self._refresh_locked()
            return self._subagent_metadata_by_session.get(subagent_session_id)

    # discovery and reading

    def _discover_root_sessions_locked(self) -> None:
        for root_session in list_root_session_dirs(self._agent_state_dir):
            if root_session.session_id in self._tail_by_session:
                continue
            self._tail_by_session[root_session.session_id] = _GrokSessionTail(
                updates_path=root_session.session_dir / UPDATES_FILENAME,
                lane=GrokLaneState(session_id=root_session.session_id, is_root=True),
            )
            self._root_session_ids.append(root_session.session_id)

    def _resolve_child_sessions_locked(self, consumed_child_ids: set[str]) -> list[_GrokSessionTail]:
        """Start tailing every named child session whose directory now exists; return the child tails not yet read."""
        for session_id in sorted(self._pending_child_session_ids):
            session_dir = get_session_dir(self._agent_state_dir, session_id)
            if session_dir is None:
                continue
            self._pending_child_session_ids.discard(session_id)
            self._tail_by_session[session_id] = _GrokSessionTail(
                updates_path=session_dir / UPDATES_FILENAME,
                lane=GrokLaneState(session_id=session_id, is_root=False),
            )
            self._store.ensure_lane(session_id)
        return [
            tail
            for tail in self._tail_by_session.values()
            if not tail.is_root and tail.session_id not in consumed_child_ids
        ]

    def _consume_root_sessions_locked(self) -> None:
        """Parse what every top-level session appended, in record-time order across sessions."""
        appended_by_session: list[list[_SessionLogRecord]] = [
            [(*log_record, tail) for log_record in self._read_appended_records_locked(tail)]
            for tail in (self._tail_by_session[session_id] for session_id in self._root_session_ids)
        ]
        for _timestamp, offset, length, record, tail in heapq.merge(*appended_by_session, key=itemgetter(0)):
            self._parse_record_locked(tail, offset, length, record)

    def _read_appended_records_locked(self, tail: _GrokSessionTail) -> list[_LogRecord]:
        """The complete records appended to the tail's log since it was last read, advancing the tail past them."""
        try:
            size = tail.updates_path.stat().st_size
        except OSError:
            return []
        if size < tail.byte_offset:
            logger.warning("The grok session log {} shrank; reading it again from the start", tail.updates_path)
            tail.byte_offset = 0
            tail.lane = GrokLaneState(session_id=tail.session_id, is_root=tail.is_root)
            tail.pending_thinking_source = None
        if size == tail.byte_offset:
            return []
        try:
            with tail.updates_path.open("rb") as f:
                f.seek(tail.byte_offset)
                appended = f.read()
        except OSError as e:
            logger.debug("Failed to read the grok session log {}: {}", tail.updates_path, e)
            return []
        complete, _fragment = split_at_last_complete_line(appended)
        records: list[_LogRecord] = []
        for offset, length, line in iter_line_spans(complete, tail.byte_offset):
            record = _decode_record(line.decode("utf-8", errors="replace"))
            if record is not None:
                records.append((record_timestamp(record), offset, length, record))
        tail.byte_offset += len(complete)
        return records

    def _parse_record_locked(self, tail: _GrokSessionTail, offset: int, length: int, record: dict[str, Any]) -> None:
        source: EventSource = (tail.updates_path, offset, length)
        lane_id = MAIN_LANE if tail.is_root else tail.session_id
        for item in parse_record(record, tail.lane, offset=offset):
            item_type = item["type"]
            if item_type == THINKING_SOURCE_MARKER_TYPE:
                tail.pending_thinking_source = source
            elif item_type == SUBAGENT_SPAWNED_MARKER_TYPE:
                self._link_subagent_locked(item)
            elif item_type == TASK_FINISHED_MARKER_TYPE:
                if tail.is_root:
                    self._on_task_finished_locked(tail.session_id, item["task_id"], item["will_wake"])
            else:
                self._ingest_locked(lane_id, item, source)
                if item.get("has_thinking") and tail.pending_thinking_source is not None:
                    self._store.set_thinking_source(item["event_id"], tail.pending_thinking_source)
                    tail.pending_thinking_source = None
                if (
                    tail.is_root
                    and item_type == SPECIAL_EVENT_TYPE
                    and item["kind"] == SpecialEventKind.TURN_STARTED.value
                ):
                    self._on_root_turn_started_locked(tail.session_id)

    def _link_subagent_locked(self, marker: dict[str, Any]) -> None:
        """Tail the child session a spawn_subagent call started, and put its metadata on the call's card."""
        child_session_id: str = marker["child_session_id"]
        metadata: dict[str, str] = marker["subagent_metadata"]
        self._subagent_metadata_by_session[child_session_id] = metadata
        if child_session_id not in self._tail_by_session:
            self._pending_child_session_ids.add(child_session_id)
        call_event_id = marker["call_event_id"]
        call_event = self._store.get_event(call_event_id) if call_event_id is not None else None
        if call_event is None:
            return
        for tool_call in call_event.get("tool_calls") or ():
            if tool_call.get("tool_call_id") == call_event_id:
                tool_call["subagent_metadata"] = metadata
        self._store.register_rebroadcast(call_event_id)

    # on-demand payload detail

    def _parse_detail(
        self, event: dict[str, Any], source_line: str | None, thinking_line: str | None
    ) -> dict[str, Any] | None:
        source_record = _decode_record(source_line)
        detail = detail_for(event, source_record) if source_record is not None else None
        thinking_record = _decode_record(thinking_line)
        thinking_detail = detail_for(event, thinking_record) if thinking_record is not None else None
        thinking = thinking_detail.get("thinking") if thinking_detail is not None else None
        if detail is None and thinking is None:
            return None
        if detail is None:
            return {"inputs_by_tool_call_id": {}, "output": None, "thinking": thinking}
        return detail if thinking is None else {**detail, "thinking": thinking}


class GrokSessionWatcher(GrokTranscriptLoader, StoreBackedWatcher):
    """Watches a grok agent's session logs and emits parsed UI events."""

    # Background work the transcript reported over since the last emit cycle that awaits no turn.
    _finished_task_ids: list[str]
    # Background work whose end starts a turn of its session, until the session's next turn starts.
    _wake_pending_task_ids_by_session: dict[str, list[str]]
    # Of those, the ones not yet reported.
    _unreported_wake_pending_task_ids: list[str]
    _finished_tasks_callback: FinishedTasksCallback | None
    _wake_pending_tasks_callback: FinishedTasksCallback | None

    @classmethod
    def build(cls, agent_info: AgentInfo, on_events: OnEventsCallback) -> "GrokSessionWatcher":
        watcher = cls.__new__(cls)
        watcher._init_store_watcher(agent_info.id, on_events)
        watcher._init_grok_state(agent_info.agent_state_dir)
        watcher._finished_task_ids = []
        watcher._wake_pending_task_ids_by_session = {}
        watcher._unreported_wake_pending_task_ids = []
        watcher._finished_tasks_callback = None
        watcher._wake_pending_tasks_callback = None
        return watcher

    def _watch_paths(self) -> tuple[Path, ...]:
        return (get_grok_home(self._agent_state_dir) / SESSIONS_DIRNAME,)

    def _on_task_finished_locked(self, session_id: str, task_id: str, will_wake: bool) -> None:
        if will_wake:
            self._wake_pending_task_ids_by_session.setdefault(session_id, []).append(task_id)
            self._unreported_wake_pending_task_ids.append(task_id)
        else:
            self._finished_task_ids.append(task_id)

    def _on_root_turn_started_locked(self, session_id: str) -> None:
        """The work that awaited the session's next turn awaits nothing now."""
        for task_id in self._wake_pending_task_ids_by_session.pop(session_id, []):
            if task_id in self._unreported_wake_pending_task_ids:
                self._unreported_wake_pending_task_ids.remove(task_id)
            self._finished_task_ids.append(task_id)

    def _before_broadcast(self) -> None:
        """Report work whose own turn is still to come ahead of the cycle's events, which a turn's end may be among."""
        with self._lock:
            wake_pending_task_ids = tuple(self._unreported_wake_pending_task_ids)
            self._unreported_wake_pending_task_ids.clear()
        if wake_pending_task_ids and self._wake_pending_tasks_callback is not None:
            self._wake_pending_tasks_callback(wake_pending_task_ids)

    def _after_broadcast(self) -> None:
        with self._lock:
            finished_task_ids = tuple(self._finished_task_ids)
            self._finished_task_ids.clear()
        if finished_task_ids and self._finished_tasks_callback is not None:
            self._finished_tasks_callback(finished_task_ids)

    def set_finished_tasks_callback(self, callback: FinishedTasksCallback) -> None:
        self._finished_tasks_callback = callback

    def set_wake_pending_tasks_callback(self, callback: FinishedTasksCallback) -> None:
        self._wake_pending_tasks_callback = callback

    def is_main_session_event(self, event: dict[str, Any]) -> bool:
        """True unless the event belongs to a subagent's child session; an event naming no session is main."""
        session_id = event.get("session_id")
        if session_id is None:
            return True
        with self._lock:
            return session_id in self._root_session_ids

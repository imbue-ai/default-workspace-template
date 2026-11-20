"""Read a grok agent's session logs into UI events, and tail the live ones.

The top-level sessions mngr records for the agent (``list_local_root_session_dirs``) share one
lane, merged by record time, because ``/new`` and ``--resume`` interleave them; each subagent's
child session, named by its parent's ``subagent_spawned`` record and found with
``find_local_session_dir``, is a lane of its own, kept out of the main stream.
:class:`GrokTranscriptLoader` is the read half, which an archived chat segment is read through;
:class:`GrokSessionWatcher` adds the live tail and the flush worker, the only code that types into
grok, which types the model bar's held commands and the messages the chat holds for it once mngr reports no turn in
flight.
"""

import heapq
import json
import math
import threading
import time
from operator import itemgetter
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.chat.activity_state import parse_iso_timestamp_to_epoch
from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.grok.model import GROK_STATE_RELATIVE_PATH
from imbue.chat.harnesses.grok.model import is_control_command_in_force
from imbue.chat.harnesses.grok.queue_tracker import GrokControlCommand
from imbue.chat.harnesses.grok.queue_tracker import GrokQueueTracker
from imbue.chat.harnesses.grok.queue_tracker import get_tracker
from imbue.chat.harnesses.grok.queue_tracker import session_token
from imbue.chat.harnesses.grok.session_parser import GrokLaneState
from imbue.chat.harnesses.grok.session_parser import SUBAGENT_SPAWNED_MARKER_TYPE
from imbue.chat.harnesses.grok.session_parser import THINKING_SOURCE_MARKER_TYPE
from imbue.chat.harnesses.grok.session_parser import UPDATES_FILENAME
from imbue.chat.harnesses.grok.session_parser import detail_for
from imbue.chat.harnesses.grok.session_parser import parse_record
from imbue.chat.harnesses.grok.session_parser import record_timestamp
from imbue.chat.harnesses.grok.turn_gate import is_ready_to_type
from imbue.chat.harnesses.model import model_state_path
from imbue.chat.harnesses.model import read_model_identity
from imbue.chat.harnesses.session_watcher import FlushResentCallback
from imbue.chat.harnesses.session_watcher import FlushSendCallback
from imbue.chat.harnesses.session_watcher import OnEventsCallback
from imbue.chat.harnesses.session_watcher import QueueSnapshotCallback
from imbue.chat.harnesses.session_watcher import ReadLifecycleStateCallback
from imbue.chat.harnesses.session_watcher import TRANSCRIPT_CLOCK_SLACK_SECONDS
from imbue.chat.harnesses.transcript_store import EventSource
from imbue.chat.harnesses.transcript_store import StoreBackedTranscriptLoader
from imbue.chat.harnesses.transcript_store import StoreBackedWatcher
from imbue.chat.harnesses.transcript_store import iter_line_spans
from imbue.chat.harnesses.transcript_store import split_at_last_complete_line
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.agents.tui_utils import is_slash_command_message
from imbue.mngr.errors import MngrError
from imbue.mngr_grok.grok_config import get_grok_home
from imbue.mngr_grok.grok_sessions import find_local_session_dir
from imbue.mngr_grok.grok_sessions import list_local_root_session_dirs

# The lane every top-level session's events share.
MAIN_LANE: Final[str] = "main"
# Grok's directory of session logs under its home.
SESSIONS_DIRNAME: Final[str] = "sessions"

# The flush worker's wait between attempts while anything is held.
_FLUSH_RETRY_SECONDS: Final[float] = 5.0
_FLUSH_JOIN_SECONDS: Final[float] = 10.0
# How long after a send the worker looks for grok's user turn holding the sent block.
_DELIVERY_WITNESS_SECONDS: Final[float] = 15.0
_DELIVERY_POLL_SECONDS: Final[float] = 0.25
# The longest a flush keeps the live emit muted, should the flush thread die before lifting it.
_EMIT_EMBARGO_CEILING_SECONDS: Final[float] = 120.0
_USER_ROLE: Final[str] = "user"

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
    if not isinstance(record, dict):
        logger.warning("Skipping a grok session-log line that holds no JSON object: {!r}", line[:200])
        return None
    return record


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

    def _discover_root_sessions_locked(self) -> None:
        for session_dir in list_local_root_session_dirs(self._agent_state_dir):
            session_id = session_dir.name
            if session_id in self._tail_by_session:
                continue
            self._tail_by_session[session_id] = _GrokSessionTail(
                updates_path=session_dir / UPDATES_FILENAME,
                lane=GrokLaneState(session_id=session_id, is_root=True),
            )
            self._root_session_ids.append(session_id)

    def _resolve_child_sessions_locked(self, consumed_child_ids: set[str]) -> list[_GrokSessionTail]:
        """Start tailing every named child session whose directory now exists; return the child tails not yet read."""
        for session_id in sorted(self._pending_child_session_ids):
            session_dir = find_local_session_dir(self._agent_state_dir, session_id)
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
        except FileNotFoundError:
            logger.trace("The grok session log {} does not exist yet", tail.updates_path)
            return []
        except OSError as e:
            logger.debug("Failed to stat the grok session log {}: {}", tail.updates_path, e)
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
            else:
                self._ingest_locked(lane_id, item, source)
                if item.get("has_thinking") and tail.pending_thinking_source is not None:
                    self._store.set_thinking_source(item["event_id"], tail.pending_thinking_source)
                    tail.pending_thinking_source = None

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
    """Watches a grok agent's session logs, emits parsed UI events, and types the messages the chat holds."""

    # The messages the chat holds for the agent, shared with its session and the stop and tap executors.
    _queue: GrokQueueTracker
    _queue_snapshot_callback: QueueSnapshotCallback | None
    _flush_send: FlushSendCallback | None
    _flush_read_lifecycle_state: ReadLifecycleStateCallback | None
    _flush_note_resent: FlushResentCallback | None
    _flush_wake: threading.Event
    _flush_stopping: threading.Event
    _flush_thread: threading.Thread | None
    # Until when the live emit holds back, so a delivered message's chip leaves before its turn arrives.
    _emit_embargo_until: float
    delivery_witness_seconds: float

    @classmethod
    def build(cls, agent_info: AgentInfo, on_events: OnEventsCallback) -> "GrokSessionWatcher":
        watcher = cls.__new__(cls)
        watcher._init_store_watcher(agent_info.id, on_events)
        watcher._init_grok_state(agent_info.agent_state_dir)
        watcher._queue = get_tracker(agent_info.agent_state_dir)
        watcher._queue_snapshot_callback = None
        watcher._flush_send = None
        watcher._flush_read_lifecycle_state = None
        watcher._flush_note_resent = None
        watcher._flush_wake = threading.Event()
        watcher._flush_stopping = threading.Event()
        watcher._flush_thread = None
        watcher._emit_embargo_until = 0.0
        watcher.delivery_witness_seconds = _DELIVERY_WITNESS_SECONDS
        return watcher

    def start(self) -> None:
        if self._flush_thread is not None:
            return
        self._queue.attach(publish=self._publish_snapshot, wake=self._flush_wake.set)
        super().start()
        self._flush_stopping.clear()
        self._flush_thread = threading.Thread(
            target=self._run_flush_worker, name=f"grok-flush-{self._agent_id}", daemon=True
        )
        self._flush_thread.start()

    def stop(self) -> None:
        self._flush_stopping.set()
        self._flush_wake.set()
        if self._flush_thread is not None:
            self._flush_thread.join(timeout=_FLUSH_JOIN_SECONDS)
            if self._flush_thread.is_alive():
                logger.warning("grok: the flush worker of {} outlived its watcher; detaching it", self._agent_id)
            self._flush_thread = None
        self._queue.detach(self._publish_snapshot)
        super().stop()

    def _watch_paths(self) -> tuple[Path, ...]:
        return (get_grok_home(self._agent_state_dir) / SESSIONS_DIRNAME,)

    def is_main_session_event(self, event: dict[str, Any]) -> bool:
        """True unless the event belongs to a subagent's child session; an event naming no session is main."""
        session_id = event.get("session_id")
        if session_id is None:
            return True
        with self._lock:
            return session_id in self._root_session_ids

    def _emit_cycle(self) -> None:
        """Broadcast what the logs gained, unless a flush holds the emit until its entries have left the queue."""
        with self._lock:
            if time.monotonic() < self._emit_embargo_until:
                return
            self._refresh_locked()
            to_send = self._filter_broadcast(self._store.take_unemitted())
        self._broadcast(to_send)

    def _emit_held_events(self) -> None:
        """Broadcast what the logs gained during a flush, then lift its embargo, so the live emit cannot go first."""
        try:
            with self._lock:
                self._refresh_locked()
                to_send = self._filter_broadcast(self._store.take_unemitted())
            self._broadcast(to_send)
        finally:
            with self._lock:
                self._emit_embargo_until = 0.0

    def set_queue_snapshot_callback(self, callback: QueueSnapshotCallback) -> None:
        self._queue_snapshot_callback = callback

    def set_flush_hooks(
        self,
        send: FlushSendCallback,
        read_lifecycle_state: ReadLifecycleStateCallback,
        note_resent: FlushResentCallback,
    ) -> None:
        self._flush_send = send
        self._flush_read_lifecycle_state = read_lifecycle_state
        self._flush_note_resent = note_resent

    def get_queued_messages(self) -> list[dict[str, Any]]:
        return self._queue.snapshot()

    def get_queued_block(self) -> str:
        return self._queue.concatenated_block()

    def clear_queue(self) -> None:
        """Drop the held messages; only for a grok session that no longer exists."""
        self._queue.clear()

    def take_unclaimed_queue(self) -> tuple[str, tuple[str, ...]]:
        return self._queue.take_unclaimed()

    def claim_queue_for_tap(self) -> tuple[str, tuple[str, ...], int]:
        return self._queue.begin_tap_claim()

    def release_tap_claim(self, claimed: tuple[str, ...], generation: int) -> None:
        self._queue.release_claim(claimed, generation)

    def notify_idle(self) -> list[dict[str, Any]]:
        """Wake the flush worker; it types on its own thread, never on the caller's."""
        self._flush_wake.set()
        return self._queue.snapshot()

    def _publish_snapshot(self, snapshot: list[dict[str, Any]]) -> None:
        callback = self._queue_snapshot_callback
        if callback is not None:
            callback(snapshot)

    def _run_flush_worker(self) -> None:
        while not self._flush_stopping.is_set():
            self._flush_wake.wait(timeout=_FLUSH_RETRY_SECONDS)
            self._flush_wake.clear()
            if self._flush_stopping.is_set():
                return
            try:
                self._attempt_flush()
            except (MngrError, OSError) as error:
                logger.opt(exception=error).warning("grok: a flush attempt failed for {}", self._agent_id)

    def _attempt_flush(self) -> None:
        """Type the next held control command, else the held block, while mngr reports grok ready to type."""
        send, read_lifecycle_state = self._flush_send, self._flush_read_lifecycle_state
        if send is None or read_lifecycle_state is None:
            return
        self._queue.set_session(session_token(self._agent_state_dir))
        self._queue.republish()
        if not self._queue.has_entries() and not self._queue.has_control_commands():
            return
        if not is_ready_to_type(read_lifecycle_state(), self._agent_state_dir):
            self._queue.demote_pending()
            return
        self._settle_interrupted_claim()
        command = self._queue.next_control_command()
        if command is not None:
            self._send_control_command(send, command)
            return
        self._flush_held_block(send)

    def _settle_interrupted_claim(self) -> None:
        """Settle the flush claim the chat app stopped during: delivered if a user turn grok logged since its typing
        began holds its block, else Queued for a later attempt."""
        interrupted = self._queue.interrupted_claim()
        if interrupted is None:
            return
        block, claimed, generation, began_at = interrupted
        with self._lock:
            self._refresh_locked()
            texts = self._user_turn_texts_since_locked(0, logged_after=began_at - TRANSCRIPT_CLOCK_SLACK_SECONDS)
        self._queue.finish_flush(claimed, generation, delivered=claimed if _is_held_by_a_turn(texts, block) else ())

    def _send_control_command(self, send: FlushSendCallback, command: GrokControlCommand) -> None:
        """Type a held control command alone, unless grok already runs it by its model_state.json."""
        identity = read_model_identity(model_state_path(self._agent_state_dir, GROK_STATE_RELATIVE_PATH))
        if is_control_command_in_force(command, identity):
            logger.debug("grok: {} already runs {!r}; not typing it", self._agent_id, command.text)
            self._queue.settle_control_command(command, is_delivered=True)
            self._flush_wake.set()
            return
        is_delivered = False
        try:
            is_delivered = send(command.text)
        finally:
            self._queue.settle_control_command(command, is_delivered=is_delivered)
        if is_delivered:
            self._flush_wake.set()
        else:
            self._queue.demote_pending()

    def _flush_held_block(self, send: FlushSendCallback) -> None:
        """Claim the held block, type it through mngr, and settle it by what mngr and grok's log show.

        A block mngr confirmed grok took is delivered, even when grok queued it behind a turn that started first and
        logs it only once that turn ends; it stays Sending until its turn shows or the witness window ends, unless it
        is a slash command, which grok runs without logging a user turn. A block mngr could not confirm is delivered
        once a user turn holding it shows, and otherwise waits for a later attempt.
        """
        with self._lock:
            self._refresh_locked()
            witness_start = self._store.total([MAIN_LANE])
        block, claimed, generation = self._queue.begin_flush()
        if not claimed:
            return
        with self._lock:
            self._emit_embargo_until = time.monotonic() + _EMIT_EMBARGO_CEILING_SECONDS
        earlier_flush_began_at = self._queue.note_flush_began(claimed, time.time())
        delivered: tuple[str, ...] = ()
        try:
            if send(block):
                delivered = claimed
                self._queue.note_typed(claimed, generation)
                if not is_slash_command_message(block):
                    self._await_turn_holding(witness_start, block)
            else:
                delivered = claimed if self._await_turn_holding(witness_start, block) else ()
        finally:
            self._queue.finish_flush(claimed, generation, delivered=delivered)
            if delivered and earlier_flush_began_at is not None and self._flush_note_resent is not None:
                self._flush_note_resent(block, earlier_flush_began_at - TRANSCRIPT_CLOCK_SLACK_SECONDS)
            if not delivered:
                logger.info("grok: {} did not show that it took the sent block", self._agent_id)
            self._emit_held_events()

    def _await_turn_holding(self, witness_start: int, block: str) -> bool:
        """Whether a user turn grok logged since the send holds the block, looking until the witness window ends."""
        deadline = time.monotonic() + self.delivery_witness_seconds
        is_held = False
        is_looking = True
        while is_looking:
            with self._lock:
                self._refresh_locked()
                texts = self._user_turn_texts_since_locked(witness_start)
            is_held = _is_held_by_a_turn(texts, block)
            is_looking = (
                not is_held and time.monotonic() < deadline and not self._flush_stopping.wait(_DELIVERY_POLL_SECONDS)
            )
        return is_held

    def _user_turn_texts_since_locked(self, start: int, logged_after: float = -math.inf) -> list[str]:
        """The text of every user turn the main lane gained from index ``start``, each complete or still growing, that
        grok logged at or after epoch ``logged_after``."""
        events = self._store.at_offset([MAIN_LANE], start, self._store.total([MAIN_LANE]) - start)
        return [
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "user_message"
            and event.get("role") == _USER_ROLE
            and (parse_iso_timestamp_to_epoch(event.get("timestamp")) or 0.0) >= logged_after
        ]


@pure
def _is_held_by_a_turn(user_turn_texts: list[str], block: str) -> bool:
    """Whether a logged user turn holds the block: alone, or after a draft someone left in grok's composer.

    grok logs a typed block as one user turn; mngr types it after any draft it finds in the composer.
    """
    sent = block.strip()
    return any(text.strip().endswith(sent) for text in user_turn_texts)

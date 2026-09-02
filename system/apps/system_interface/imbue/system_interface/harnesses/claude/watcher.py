"""Watch raw Claude session JSONL files and emit parsed events.

Built on the shared :class:`~imbue.system_interface.harnesses.transcript_store` scaffolding:
one lane per session file (main sessions plus subagents), every parsed event fully
resident, and per-event source byte ranges kept beside the store for the on-demand payload
reads.

What stays claude-specific:

* **Discovery.** Main sessions come from ``claude_session_id_history`` in first-mention
  order (a resume re-lists the same id; a late-found session is inserted by its history
  position so the latest-session gates below stay aimed at the live session). Subagent
  sessions live under ``<session>/subagents/`` beside each main file, with a
  ``<id>.meta.json`` carrying display metadata plus the spawn-time ``toolUseId`` link.

* **The queue feed.** Claude records its live queue as out-of-band ``queue-operation``
  ledger lines. Only the LATEST main session's ledger mirrors the live process's queue,
  and -- because ``--resume`` re-appends to the same file -- enqueues stamped before the
  ``claude_process_started`` marker belong to a dead process and are excluded on replay
  (see :func:`_is_dead_epoch_enqueue`).

* **Subagent enrichment.** A parent Agent tool_call's ``subagent_metadata`` can land after
  the parent was already emitted (the subagent's meta.json shows up a cycle later, or its
  tool_result later still). Because events are resident, enrichment simply mutates the
  parent in place; a parent still missing metadata sits in a pending set retried each
  refresh, and a change registers a re-broadcast so the client upgrades its held card.

Lock discipline (owned by the shared base): the one lock is held across discovery, file
reads, and parsing -- cheap and incremental -- but never across the ``on_events`` fan-out
or the queue-snapshot callback.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from typing import Callable

from loguru import logger as _loguru_logger

from imbue.system_interface.activity_state import parse_iso_timestamp_to_epoch
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.claude.activity import ClaudeActivityTracker
from imbue.system_interface.harnesses.claude.queue_tracker import ClaudeQueueTracker
from imbue.system_interface.harnesses.claude.session_parser import QueueSignal
from imbue.system_interface.harnesses.claude.session_parser import QueueSignalKind
from imbue.system_interface.harnesses.claude.session_parser import parse_line_detail
from imbue.system_interface.harnesses.claude.session_parser import parse_lines
from imbue.system_interface.harnesses.claude.session_parser import parse_queue_signals
from imbue.system_interface.harnesses.session_watcher import OnEventsCallback
from imbue.system_interface.harnesses.transcript_store import EventSource
from imbue.system_interface.harnesses.transcript_store import StoreBackedWatcher
from imbue.system_interface.harnesses.transcript_store import iter_line_spans
from imbue.system_interface.harnesses.transcript_store import split_at_last_complete_line

logger = _loguru_logger


def _is_dead_epoch_enqueue(signal: QueueSignal, process_epoch_started_at: float | None) -> bool:
    """True iff this queue signal is an enqueue from a previous claude process's epoch.

    ``claude --resume`` RE-APPENDS to the same session JSONL, so the latest main
    session's ledger can span process restarts. An enqueue a killed process never
    resolved dangles in that ledger forever; a full replay (backend-restart
    priming, the truncation reset) would re-derive it as still queued -- a ghost
    entry the working->IDLE backstop later silently evaporates. The live
    process's start boundary is the ``claude_process_started`` marker mtime
    (touched by the SessionStart hook on every startup/resume); an enqueue
    stamped before it died with its process and must not feed the populator.

    LEAVE signals are never excluded: a dead epoch's leave nets against a dead
    epoch's (excluded) enqueue, and resolving against an empty set is a harmless
    no-op, so FIFO alignment within the live epoch holds. A missing marker or an
    unparseable stamp keeps the enqueue -- only records positively known to
    predate the live process are dropped.
    """
    if signal.kind != QueueSignalKind.ENQUEUE or process_epoch_started_at is None:
        return False
    enqueue_at = parse_iso_timestamp_to_epoch(signal.timestamp)
    return enqueue_at is not None and enqueue_at < process_epoch_started_at


class _FileCursor:
    """Incremental read state for one session JSONL file."""

    __slots__ = ("session_id", "file_path", "byte_offset_consumed", "last_mtime")

    def __init__(self, session_id: str, file_path: Path) -> None:
        self.session_id = session_id
        self.file_path = file_path
        self.byte_offset_consumed = 0
        self.last_mtime = 0.0


class ClaudeSessionWatcher(StoreBackedWatcher):
    """Watches all session files for a single mngr agent and emits parsed events."""

    @classmethod
    def build(cls, agent_info: AgentInfo, on_events: OnEventsCallback) -> "ClaudeSessionWatcher":
        """Build from the agent record. Claude needs its per-agent config dir, which is
        where Claude Code writes the session JSONL files this watcher tails."""
        return cls(
            agent_id=agent_info.id,
            agent_state_dir=agent_info.agent_state_dir,
            claude_config_dir=agent_info.claude_config_dir,
            on_events=on_events,
        )

    def __init__(
        self,
        agent_id: str,
        agent_state_dir: Path,
        claude_config_dir: Path,
        on_events: Callable[[str, list[dict[str, Any]]], None],
    ) -> None:
        self._init_store_watcher(agent_id, on_events)
        self._agent_state_dir = agent_state_dir
        self._claude_config_dir = claude_config_dir

        # All guarded by the base's lock (held through _refresh_locked).
        self._cursor_by_session: dict[str, _FileCursor] = {}
        self._main_session_ids: list[str] = []
        self._tool_name_by_call_id: dict[str, str] = {}
        self._subagent_metadata: dict[str, dict[str, str]] = {}  # sub_id -> {agent_type, description}
        # sub_ids whose meta.json we've already determined is permanently malformed.
        # Used to log the warning once per file instead of once per poll cycle.
        self._subagent_meta_read_failed: set[str] = set()
        # sub_id -> the parent Agent tool_use id, read from the subagent's `<id>.meta.json`
        # `toolUseId` field: the direct, spawn-time link between a parent Agent tool_call
        # and its subagent, written before any tool_result lands, so running subagents get
        # the rich card. (The subagent jsonl's own first line carries no usable parent
        # pointer, so the meta.json is the only pre-completion source.)
        self._subagent_tool_use_id: dict[str, str] = {}
        # tool_call_id -> subagent_id, accumulated from parent tool_results as they land.
        # The fallback link for sessions recorded on Claude Code versions whose meta.json
        # omits toolUseId; only available once the subagent finishes.
        self._subagent_id_by_tool_call: dict[str, str] = {}
        # event_ids of resident assistant messages with at least one Agent tool_call still
        # missing subagent_metadata. Retried on every refresh: linkage can land any number
        # of cycles after the parent, and a change re-broadcasts the (mutated-in-place)
        # parent so the client's card upgrades without a page refresh. An id whose linkage
        # never arrives (the subagent's transcript is gone) just stays here -- the retry is
        # a few map lookups, and the set is bounded by the transcript's Agent calls.
        self._pending_enrichment_ids: set[str] = set()

        # The queued-message populator (the only harness-specific queue code) and the last
        # snapshot pushed to the agent manager, compared so an unchanged queue pushes
        # nothing. The callback is set once before ``start`` and read without the lock.
        self._queue_tracker = ClaudeQueueTracker.build()
        self._last_broadcast_queue_snapshot: list[dict[str, str]] = []
        self._queue_snapshot_callback: Callable[[list[dict[str, Any]]], None] | None = None

    # -- base hooks -----------------------------------------------------------------------

    def _watch_paths(self) -> tuple[Path, ...]:
        # The projects tree (recursive: every session file and subagent dir under it wakes
        # the loop, including ones created later) plus the history file's directory.
        return (self._claude_config_dir / "projects", self._agent_state_dir / "claude_session_id_history")

    def _refresh_locked(self) -> None:
        self._discover_sessions_locked()
        for cursor in list(self._cursor_by_session.values()):
            if cursor.file_path.exists():
                self._consume_file_locked(cursor)
        self._retry_pending_enrichment_locked()

    def _selected_lane_ids_locked(self, session_id: str | None) -> list[str]:
        if session_id is not None:
            return [session_id] if self._store.has_lane(session_id) else []
        # Main sessions in history (chronological) order. Resumed sessions do not overlap
        # in time, so this order matches the merged timestamp order.
        return [sid for sid in self._main_session_ids if self._store.has_lane(sid)]

    def _before_broadcast(self) -> None:
        # A3b ordering: a Queued->Delivered message leaves the queue and appears as a
        # committed transcript turn in the SAME cycle (its LEAVE record and its ``user``
        # record ride the same file). Push the queue snapshot (the chip REMOVAL) before the
        # transcript turn is broadcast, so the message is never a chip and a turn at once.
        self._broadcast_queue_snapshot_if_changed()

    # -- discovery ------------------------------------------------------------------------

    def _discover_sessions_locked(self) -> None:
        """Discover this agent's main sessions and any subagent sessions under them."""
        self._discover_main_sessions_from_history_locked()
        # Discover subagent sessions for ALL known main sessions (not just newly discovered
        # ones): subagent files may appear after the parent session is first discovered,
        # and this must run even when the history file is gone (a rotated/replaced agent
        # can leave a main session watchable while its history file is gone).
        for cursor in list(self._cursor_by_session.values()):
            if cursor.session_id in self._main_session_ids:
                self._discover_subagent_sessions_locked(cursor.session_id, cursor.file_path)

    def _discover_main_sessions_from_history_locked(self) -> None:
        """Register any not-yet-known main sessions listed in claude_session_id_history."""
        history_file = self._agent_state_dir / "claude_session_id_history"
        if not history_file.exists():
            return
        try:
            lines = history_file.read_text().splitlines()
        except OSError as e:
            logger.debug("Failed to read session history file {}: {}", history_file, e)
            return

        # One history position per session id, keyed to its FIRST mention: a resume
        # re-lists the same id, and chronological order is the order ids first appeared.
        history_positions: dict[str, int] = {}
        for line in lines:
            parts = line.strip().split()
            if parts:
                history_positions.setdefault(parts[0], len(history_positions))

        for session_id in history_positions:
            if session_id in self._cursor_by_session:
                continue
            # A just-created session's file may not be on disk yet. Discovery runs
            # synchronously on the HTTP read paths, so a miss must never wait here: leave
            # the session unregistered and let the next refresh retry.
            file_path = self._find_session_file(session_id)
            if file_path is None:
                logger.debug("Session file not found for {}, will retry on next cycle", session_id)
                continue
            self._cursor_by_session[session_id] = _FileCursor(session_id, file_path)
            self._store.ensure_lane(session_id)
            insert_position = self._main_session_insert_position_locked(session_id, history_positions)
            self._main_session_ids.insert(insert_position, session_id)
            # A NEW latest main session means the claude process restarted into a fresh
            # session, so anything the previous session's ledger fed is a dead process's
            # residue -- purge it now rather than waiting for the new session to emit a
            # queue signal. A late-FOUND older session must NOT reset: the live queue
            # derived from the still-latest session would be dropped with no replay left
            # to rebuild it.
            if insert_position == len(self._main_session_ids) - 1:
                self._queue_tracker.reset()

    def _main_session_insert_position_locked(self, session_id: str, history_positions: dict[str, int]) -> int:
        """The index in ``_main_session_ids`` that keeps history order.

        A session can be FOUND late -- its file was not yet on disk when a later session
        was registered -- so a plain append would misorder the merged timeline and, worse,
        misdirect the latest-session gates (:meth:`_is_latest_main_session_locked` /
        :meth:`get_latest_main_session_file`) at a dead session. A registered session the
        current history file no longer lists sorts as oldest.
        """
        position = history_positions.get(session_id, -1)
        for index, known_session_id in enumerate(self._main_session_ids):
            if history_positions.get(known_session_id, -1) > position:
                return index
        return len(self._main_session_ids)

    def _discover_subagent_sessions_locked(self, parent_session_id: str, parent_file_path: Path) -> None:
        """Discover subagent session files under ``<session_id>/subagents/`` and read each
        ``<id>.meta.json`` for display metadata plus the parent ``toolUseId`` link."""
        subagents_dir = parent_file_path.parent / parent_session_id / "subagents"
        if not subagents_dir.exists():
            return
        for jsonl_file in subagents_dir.glob("*.jsonl"):
            sub_id = jsonl_file.stem
            if sub_id not in self._cursor_by_session:
                self._cursor_by_session[sub_id] = _FileCursor(sub_id, jsonl_file)
                self._store.ensure_lane(sub_id)
            if sub_id in self._subagent_metadata or sub_id in self._subagent_meta_read_failed:
                continue
            meta_file = jsonl_file.with_suffix(".meta.json")
            if not meta_file.exists():
                continue
            # Retry on each pass while the read fails with OSError (transient: mid-write, a
            # momentary permission glitch). Give up after a JSONDecodeError (truly malformed
            # -- won't self-heal) so the warning logs once instead of once per poll.
            try:
                meta = json.loads(meta_file.read_text())
            except json.JSONDecodeError as exc:
                logger.warning("Subagent meta.json is not valid JSON, giving up: {}: {}", meta_file, exc)
                self._subagent_meta_read_failed.add(sub_id)
                continue
            except OSError as exc:
                logger.debug("Failed to read subagent meta.json {}: {}", meta_file, exc)
                continue
            self._subagent_metadata[sub_id] = {
                "agent_type": meta.get("agentType", ""),
                "description": meta.get("description", ""),
                "session_id": sub_id,
            }
            # toolUseId points directly at the parent Agent tool_use, giving the running
            # subagent its rich card before any tool_result lands. Absent on older Claude
            # Code versions, which fall back to tool_result linkage.
            tool_use_id = meta.get("toolUseId")
            if isinstance(tool_use_id, str) and tool_use_id:
                self._subagent_tool_use_id[sub_id] = tool_use_id

    def _find_session_file(self, session_id: str) -> Path | None:
        """Search for a session JSONL file under the Claude projects directory."""
        projects_dir = self._claude_config_dir / "projects"
        if not projects_dir.exists():
            return None
        target_name = f"{session_id}.jsonl"
        for root, _dirs, files in os.walk(str(projects_dir)):
            if target_name in files:
                return Path(root) / target_name
        return None

    # -- consumption ----------------------------------------------------------------------

    def _is_latest_main_session_locked(self, session_id: str) -> bool:
        """The queue-feed gate: the live claude process's in-memory queue can only live in
        the LATEST main session's ledger, so only that session feeds the queue populator --
        a dead session's dangling enqueues must never re-derive. Event routing keeps plain
        membership; only the queue feed is scoped this tightly."""
        return bool(self._main_session_ids) and self._main_session_ids[-1] == session_id

    def _read_process_epoch_started_at(self) -> float | None:
        """The live claude process's start boundary (the ``claude_process_started`` marker
        mtime, touched by the SessionStart hook on every startup/resume), or None."""
        marker = self._agent_state_dir / ClaudeActivityTracker.marker_filename
        try:
            return marker.stat().st_mtime
        except OSError:
            return None

    def _consume_file_locked(self, cursor: _FileCursor) -> None:
        """Parse the bytes appended to one session file since its cursor into the store."""
        try:
            stat = cursor.file_path.stat()
        except OSError as e:
            logger.debug("Failed to stat session file {}: {}", cursor.file_path, e)
            return
        current_size = stat.st_size
        current_mtime = stat.st_mtime

        # Truncation / rotation: the file shrank below what was consumed, so the cursor is
        # stale. Drop the lane's events (their source ranges dangle) and re-read from the
        # start; ingest dedups by id, and changed content supersedes in place. A rewritten
        # latest-main-session file must reset the queue populator too -- otherwise
        # re-feeding the same enqueues would double the pending set.
        if current_size < cursor.byte_offset_consumed:
            self._store.reset_lane(cursor.session_id)
            cursor.byte_offset_consumed = 0
            if self._is_latest_main_session_locked(cursor.session_id):
                self._queue_tracker.reset()

        if current_size == cursor.byte_offset_consumed and current_mtime == cursor.last_mtime:
            return

        try:
            with open(cursor.file_path, "rb") as f:
                f.seek(cursor.byte_offset_consumed)
                new_data = f.read()
        except OSError as e:
            logger.debug("Failed to read session file {}: {}", cursor.file_path, e)
            return

        complete, _fragment = split_at_last_complete_line(new_data)
        if not complete:
            # Only a partial trailing line so far; leave the cursor and re-read once the
            # writer flushes the rest.
            return

        # Queue signals ride the main session's ledger, never a subagent's, and only the
        # LATEST main session's ledger mirrors the live process's queue. This is the single
        # feed point, so every replay path -- priming, the truncation reset above, HTTP-read
        # refreshes, the poll loop -- inherits the scope, including the process-epoch
        # boundary that keeps a dead process's dangling enqueues from re-deriving.
        is_latest_main_session = self._is_latest_main_session_locked(cursor.session_id)
        process_epoch_started_at = self._read_process_epoch_started_at() if is_latest_main_session else None

        for byte_offset, byte_len, line_bytes in iter_line_spans(complete, cursor.byte_offset_consumed):
            try:
                decoded_line = line_bytes.decode("utf-8")
            except UnicodeDecodeError as e:
                logger.warning("UTF-8 decode error in session file {}: {}", cursor.file_path, e)
                continue
            if is_latest_main_session:
                queue_signal = parse_queue_signals(decoded_line)
                if queue_signal is not None and not _is_dead_epoch_enqueue(queue_signal, process_epoch_started_at):
                    self._queue_tracker.consume(queue_signal)
            line_events = parse_lines(
                decoded_line.splitlines(),
                existing_event_ids=None,
                tool_name_by_call_id=self._tool_name_by_call_id,
                session_id=cursor.session_id,
            )
            source: EventSource = (cursor.file_path, byte_offset, byte_len)
            for event in line_events:
                self._store.ingest(cursor.session_id, event, source)
                self._note_linkage_and_enrich_locked(event)

        cursor.byte_offset_consumed += len(complete)
        cursor.last_mtime = current_mtime

    # -- subagent enrichment --------------------------------------------------------------

    def _note_linkage_and_enrich_locked(self, event: dict[str, Any]) -> None:
        """Record any linkage a just-ingested event carries and enrich it if applicable."""
        if event.get("type") == "tool_result" and "subagent_id" in event:
            self._subagent_id_by_tool_call.setdefault(event["tool_call_id"], event["subagent_id"])
            return
        if event.get("type") != "assistant_message":
            return
        if not any(tc.get("tool_name") == "Agent" for tc in event.get("tool_calls", [])):
            return
        self._apply_enrichment_locked(event)
        if not _is_fully_enriched(event):
            self._pending_enrichment_ids.add(event["event_id"])

    def _apply_enrichment_locked(self, event: dict[str, Any]) -> bool:
        """Attach subagent_metadata to the event's Agent tool_calls where linkage is known.

        Mutates the resident event in place (the same dict the store serves and the client
        holds a copy of) and returns whether anything changed. Linkage precedence: the
        spawn-time ``toolUseId`` from each subagent's meta.json first, the tool_result's
        ``subagent_id`` as the fallback.
        """
        subagent_by_tool_call: dict[str, str] = {}
        for sub_id, tool_use_id in self._subagent_tool_use_id.items():
            subagent_by_tool_call[tool_use_id] = sub_id
        for tool_call_id, subagent_id in self._subagent_id_by_tool_call.items():
            subagent_by_tool_call.setdefault(tool_call_id, subagent_id)

        changed = False
        for tc in event.get("tool_calls", []):
            if tc.get("tool_name") != "Agent" or "subagent_metadata" in tc:
                continue
            sub_id = subagent_by_tool_call.get(tc.get("tool_call_id", ""))
            if not sub_id:
                continue
            # The agentId in tool results is bare (e.g. "af25b729465418580") but session
            # files are named "agent-<id>.jsonl", so metadata is keyed by "agent-<id>";
            # try both forms.
            metadata = self._subagent_metadata.get(sub_id) or self._subagent_metadata.get(f"agent-{sub_id}")
            if metadata:
                tc["subagent_metadata"] = metadata
                changed = True
        return changed

    def _retry_pending_enrichment_locked(self) -> None:
        """Re-attempt enrichment for parents still missing metadata; a change re-broadcasts
        the mutated parent so the client's card upgrades live."""
        for event_id in list(self._pending_enrichment_ids):
            event = self._store.get_event(event_id)
            if event is None:
                self._pending_enrichment_ids.discard(event_id)
                continue
            if self._apply_enrichment_locked(event):
                self._store.register_rebroadcast(event_id)
            if _is_fully_enriched(event):
                self._pending_enrichment_ids.discard(event_id)

    # -- main/subagent routing ------------------------------------------------------------

    def is_main_session_event(self, event: dict[str, Any]) -> bool:
        """True if an event belongs to a main session rather than a subagent session.

        Events with no ``session_id`` (e.g. plugin-injected application events) are treated
        as main so they keep reaching the main stream; subagent-session events are
        delivered only through the per-subagent stream.
        """
        session_id = event.get("session_id")
        if session_id is None:
            return True
        with self._lock:
            return session_id in self._main_session_ids

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        """Get metadata for a subagent by its session ID."""
        with self._lock:
            self._discover_sessions_locked()
            return self._subagent_metadata.get(subagent_session_id)

    def get_latest_main_session_file(self) -> Path | None:
        """The JSONL path of the latest main session (the live process's session), or None.

        The live claude process's queue and in-flight turn live in the newest main session
        file -- a fresh start rotates into a new session id, and a ``--resume`` re-appends
        to the newest one. The shoulder tap uses this to take a byte-size baseline before
        delivering the flush chord and to read the raw post-chord tail afterwards.
        """
        with self._lock:
            self._discover_sessions_locked()
            if not self._main_session_ids:
                return None
            cursor = self._cursor_by_session.get(self._main_session_ids[-1])
        if cursor is not None and cursor.file_path.exists():
            return cursor.file_path
        return None

    # -- queued messages ------------------------------------------------------------------

    def set_queue_snapshot_callback(self, callback: Callable[[list[dict[str, Any]]], None]) -> None:
        """Register the sink the watcher pushes each new queued-message snapshot to."""
        self._queue_snapshot_callback = callback

    def get_queued_messages(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._queue_tracker.snapshot()

    def get_queued_block(self) -> str:
        with self._lock:
            return self._queue_tracker.concatenated_block()

    def clear_queue(self) -> None:
        """Drop the queued set (a flush restart invalidated it) and push the empty snapshot."""
        with self._lock:
            self._queue_tracker.clear()
        self._broadcast_queue_snapshot_if_changed()

    def notify_idle(self) -> list[dict[str, Any]]:
        """Apply the working->IDLE backstop and return the resulting (empty) snapshot.

        The caller (the agent manager, on a working->IDLE transition) folds the returned
        snapshot into the same broadcast that carries the IDLE activity state, so this does
        not push a broadcast of its own -- it only records the cleared snapshot as
        broadcast so the poll loop does not re-push it.
        """
        with self._lock:
            self._queue_tracker.on_idle()
            snapshot = self._queue_tracker.snapshot()
            self._last_broadcast_queue_snapshot = snapshot
        return snapshot

    def _broadcast_queue_snapshot_if_changed(self) -> None:
        """Push the live queued snapshot to the registered sink when it has changed. The
        comparison runs under the lock; the callback fan-out runs outside it."""
        callback = self._queue_snapshot_callback
        if callback is None:
            return
        with self._lock:
            snapshot = self._queue_tracker.snapshot()
            if snapshot == self._last_broadcast_queue_snapshot:
                return
            self._last_broadcast_queue_snapshot = snapshot
        callback(snapshot)

    # -- on-demand payload detail ---------------------------------------------------------

    def _parse_detail(
        self, event: dict[str, Any], source_line: str | None, thinking_line: str | None
    ) -> dict[str, Any] | None:
        if source_line is None:
            return None
        return parse_line_detail(source_line).get(event["event_id"])

    def _find_detail_source_fallback(self, event: dict[str, Any]) -> str | None:
        """Scan the event's own session file for the line carrying its payloads (the
        recorded byte range went stale: the file was rewritten under us)."""
        session_id = event.get("session_id")
        if not isinstance(session_id, str):
            return None
        with self._lock:
            cursor = self._cursor_by_session.get(session_id)
        if cursor is None:
            return None
        event_id = event["event_id"]
        try:
            with cursor.file_path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.strip() and event_id in parse_line_detail(line):
                        return line
        except OSError:
            return None
        return None


def _is_fully_enriched(event: dict[str, Any]) -> bool:
    """True if every Agent tool_call in ``event`` already carries subagent_metadata --
    the condition for retiring a parent from the enrichment retry set."""
    agent_tool_calls = [tc for tc in event.get("tool_calls", []) if tc.get("tool_name") == "Agent"]
    return bool(agent_tool_calls) and all("subagent_metadata" in tc for tc in agent_tool_calls)

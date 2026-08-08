"""Tail a pi agent's native session JSONL and emit UI events.

The pi analogue of :class:`codex.watcher.CodexSessionWatcher`. It tails pi's OWN
on-disk session file in real time -- the same file pi writes as it works, verified
to be written incrementally per record -- parses each line via
:func:`pi_coding.session_parser.parse_record`, dedups by ``event_id`` (pi's stable
record id), and fans new events out through ``on_events``. It reads pi's live file,
not mngr's ``logs/<type>_transcript`` mirror, the same way the codex watcher reads
codex's live rollout rather than its mirror.

Which file is live rotates only on ``/new`` (a fresh session file); a stop/start
resume (`pi --session <file>`) reuses and appends to the same file. Like codex
(which follows ``codex_transcript_path``), we follow the live file via a marker:
the lifecycle extension writes its absolute path to
``<agent_state_dir>/pi_session_file`` on ``session_start`` / ``session_switch``.
Each cycle we re-read that marker; when it points somewhere new we switch files
(from the new file's start), keeping the accumulated events / dedup index.

Queued messages (the shoulder tap) are populated from mngr's ``pi_inbox`` -- the
file mngr appends each outgoing message to before the extension injects it -- via
:class:`pi_coding.queue_tracker.PiQueueTracker`. The watcher tails ``pi_inbox``
alongside the session file: each new inbox line is an enqueue, each drained
``user_message`` a leave. This needs no binary patch (unlike codex's queued-input
sidecar) and no ledger reconstruction (unlike claude): the inbox already is the
enqueue ledger.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from typing import Callable

from loguru import logger as _loguru_logger

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.path_watch import PathWatcher
from imbue.system_interface.harnesses.pi_coding.queue_tracker import PiQueueTracker
from imbue.system_interface.harnesses.pi_coding.session_parser import parse_record
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher
from imbue.system_interface.harnesses.session_watcher import OnEventsCallback
from imbue.system_interface.harnesses.session_watcher import QueueSnapshotCallback

logger = _loguru_logger

# Marker holding the live native session file's absolute path (rewritten on
# session_start / session_switch, so it follows a /new rotation). Kept in sync with
# SESSION_FILE_NAME in mngr_pi_coding's plugin.py / lifecycle extension.
_MARKER_RELATIVE = Path("pi_session_file")
# The stable dir every native session file (across /new rotation) lives under -- what a
# recursive watch targets. Kept in sync with the plugin's PI_CODING_AGENT_DIR layout.
_SESSIONS_RELATIVE = Path("plugin") / "pi_coding" / "sessions"
# mngr's outgoing-message ledger (the enqueue source for the queue). Kept in sync with
# _INBOX_FILE_NAME in plugin.py.
_INBOX_RELATIVE = Path("pi_inbox")

# The live queue snapshot is only pushed to the frontend once it has been STABLE for this
# long, so a message sent to an idle agent -- which lands in the inbox and then drains into
# a real turn within a second or so -- never flickers as "queued" first. A message that is
# genuinely parked (behind a running turn) stays put past the window and surfaces normally,
# just this much later. Explicit flush / idle-backstop transitions bypass the debounce.
_QUEUE_DEBOUNCE_SECONDS: float = 2.0


def read_marker_session_path(marker_path: Path) -> Path | None:
    """The absolute session-file path recorded in the marker, or None when absent/empty
    (the agent has not started a session yet)."""
    try:
        raw = marker_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return Path(raw) if raw else None


class PiSessionWatcher(AgentSessionWatcher):
    """Watches a pi agent's native session file (+ inbox) and emits parsed UI events."""

    # Instance attributes declared at class level so a `build()` classmethod (no
    # __init__) can assign them while the type checker still resolves every access.
    _agent_id: str
    _marker_path: Path
    _sessions_dir: Path
    _inbox_path: Path
    _on_events: Callable[[str, list[dict[str, Any]]], None]
    _lock: threading.Lock
    _events: list[dict[str, Any]]
    _event_index: dict[str, int]
    _superseded_pending: dict[str, dict[str, Any]]
    _current_path: Path | None
    _byte_offset: int
    _emitted_count: int
    # A trailing partial line carried across reads as RAW BYTES: a multi-byte UTF-8
    # character split across a read boundary must be completed before decoding.
    _partial: bytes
    # Queue: the populator, the inbox cursor + trailing partial, a monotonic line counter
    # (for stable queued ids), the snapshot sink, and the last snapshot pushed (so a
    # cycle that leaves the queue unchanged pushes nothing).
    _queue_tracker: PiQueueTracker
    _inbox_offset: int
    _inbox_partial: bytes
    _inbox_line_count: int
    _queue_snapshot_callback: QueueSnapshotCallback | None
    _last_queue_snapshot: list[dict[str, str]]
    # The debounce: the snapshot currently waiting out the stability window, and the
    # monotonic time it first appeared. ``_now`` is the clock (injectable in tests).
    _pending_queue_snapshot: list[dict[str, str]]
    _pending_queue_since: float
    _now: Callable[[], float]
    _path_watcher: PathWatcher | None

    @classmethod
    def build(cls, agent_info: AgentInfo, on_events: OnEventsCallback) -> "PiSessionWatcher":
        """Build from the agent record. pi needs only the state dir: its session file and
        inbox both live under it."""
        agent_state_dir = agent_info.agent_state_dir
        self = cls.__new__(cls)
        self._agent_id = agent_info.id
        self._marker_path = agent_state_dir / _MARKER_RELATIVE
        self._sessions_dir = agent_state_dir / _SESSIONS_RELATIVE
        self._inbox_path = agent_state_dir / _INBOX_RELATIVE
        self._on_events = on_events

        # Guards the in-memory transcript mirror, the tail cursor, and the queue populator.
        # Held across the (cheap, incremental) file reads, never across the ``on_events`` /
        # queue-snapshot callbacks -- the same discipline the codex/claude watchers follow.
        self._lock = threading.Lock()
        self._events = []
        self._event_index = {}
        self._superseded_pending = {}
        self._current_path = None
        self._byte_offset = 0
        self._emitted_count = 0
        self._partial = b""

        self._queue_tracker = PiQueueTracker.build()
        self._inbox_offset = 0
        self._inbox_partial = b""
        self._inbox_line_count = 0
        self._queue_snapshot_callback = None
        self._last_queue_snapshot = []
        self._pending_queue_snapshot = []
        self._pending_queue_since = 0.0
        self._now = time.monotonic

        self._path_watcher = None
        return self

    def start(self) -> None:
        """Start tailing in a background thread.

        The watch loop is the shared :class:`PathWatcher` on the sessions dir (recursive,
        so appends to whichever session file is live wake it without re-scheduling on a
        /new rotation) plus the inbox file. It calls ``_emit_unsent`` once at start -- to
        broadcast whatever already exists, since the agent may have run before the UI
        connected -- and on every filesystem wake or poll timeout.
        """
        self._path_watcher = PathWatcher.build((self._sessions_dir, self._inbox_path), self._emit_unsent)
        self._path_watcher.start()

    def stop(self) -> None:
        """Stop tailing."""
        if self._path_watcher is not None:
            self._path_watcher.stop()

    def _refresh(self) -> None:
        """Bring the in-memory transcript + queue up to date with disk. Incremental, so a
        caught-up refresh reads no bytes. Called by the loop AND every read method, mirroring
        the codex watcher, so a read never depends on the loop having run."""
        self._consume_new_lines()

    def _emit_unsent(self) -> None:
        """Refresh, broadcast unsent events, then push the queue snapshot if it changed."""
        self._refresh()
        with self._lock:
            pending = self._superseded_pending
            self._superseded_pending = {}
            to_send = list(pending.values()) + self._events[self._emitted_count :]
            self._emitted_count = len(self._events)
            snapshot = self._queue_tracker.snapshot()
        if to_send:
            self._on_events(self._agent_id, to_send)
        self._push_queue_snapshot_debounced(snapshot)

    def _push_queue_snapshot_debounced(self, snapshot: list[dict[str, str]]) -> None:
        """Push ``snapshot`` only once it has held stable for the debounce window.

        Each cycle re-reads the snapshot; a change restarts the window. The push fires when
        the current snapshot has been unchanged for ``_QUEUE_DEBOUNCE_SECONDS`` -- so a
        message that lands in the inbox and drains into a real turn within the window (the
        idle-agent case) is superseded before it is ever pushed. The 1s poll guarantees the
        window is re-evaluated even with no further filesystem events.
        """
        now = self._now()
        if snapshot != self._pending_queue_snapshot:
            self._pending_queue_snapshot = snapshot
            self._pending_queue_since = now
        if now - self._pending_queue_since >= _QUEUE_DEBOUNCE_SECONDS:
            self._push_queue_snapshot(snapshot)

    def _push_queue_snapshot(self, snapshot: list[dict[str, str]]) -> None:
        """Push ``snapshot`` to the sink iff it differs from the last one pushed.

        Also syncs the debounce state so an explicit push (flush) settles the window rather
        than fighting it.
        """
        self._pending_queue_snapshot = snapshot
        self._pending_queue_since = self._now()
        if snapshot == self._last_queue_snapshot:
            return
        self._last_queue_snapshot = snapshot
        if self._queue_snapshot_callback is not None:
            self._queue_snapshot_callback(snapshot)

    def _consume_new_lines(self) -> None:
        """Read bytes appended since the last cursor: the inbox first (so an enqueue exists
        before the turn it drains into is counted as a leave), then the live session file
        (following a /new rotation via the marker)."""
        with self._lock:
            self._consume_inbox()
            target = read_marker_session_path(self._marker_path)
            if target is None:
                return
            if target != self._current_path:
                # First resolution or a /new rotation. Tail the new file from its start.
                # Keep _events/_event_index (a resume appends to the SAME file, so this only
                # trips on /new -- keeping the accumulated transcript is harmless and the
                # dedup index makes a re-emit a no-op). pi's followUp queue does not survive
                # /new, so clear the queued set.
                if self._current_path is not None:
                    self._queue_tracker.clear()
                self._current_path = target
                self._byte_offset = 0
                self._partial = b""

            try:
                size = target.stat().st_size
            except OSError:
                # marker points at a not-yet-created file; retry next cycle.
                return
            # Native session files are append-only; a shrink is unexpected -- re-read from
            # the start (id-based dedup drops the re-emitted events).
            if size < self._byte_offset:
                self._byte_offset = 0
                self._partial = b""
            if size == self._byte_offset and not self._partial:
                return

            try:
                with target.open("rb") as f:
                    f.seek(self._byte_offset)
                    raw = f.read()
            except OSError:
                logger.debug("pi watcher: failed to read {}", target)
                return
            self._byte_offset += len(raw)

            byte_lines = (self._partial + raw).split(b"\n")
            self._partial = byte_lines.pop()
            for byte_line in byte_lines:
                stripped = byte_line.decode("utf-8", errors="replace").strip()
                if not stripped:
                    continue
                for event in self._adapt_line(stripped):
                    self._ingest_event(event)

    def _consume_inbox(self) -> None:
        """Read new lines from ``pi_inbox`` and enqueue each into the queue populator. Must
        hold ``_lock``. The inbox is append-only and mngr-owned; a shrink (unexpected)
        re-reads from the start, and the queue set is rebuilt from position on the next
        leaves."""
        path = self._inbox_path
        try:
            size = path.stat().st_size
        except OSError:
            # No inbox yet (nothing sent): nothing to do.
            return
        if size < self._inbox_offset:
            self._inbox_offset = 0
            self._inbox_partial = b""
            self._inbox_line_count = 0
            self._queue_tracker.reset()
        if size == self._inbox_offset and not self._inbox_partial:
            return
        try:
            with path.open("rb") as f:
                f.seek(self._inbox_offset)
                raw = f.read()
        except OSError:
            logger.debug("pi watcher: failed to read inbox {}", path)
            return
        self._inbox_offset += len(raw)
        byte_lines = (self._inbox_partial + raw).split(b"\n")
        self._inbox_partial = byte_lines.pop()
        for byte_line in byte_lines:
            stripped = byte_line.decode("utf-8", errors="replace").strip()
            if not stripped:
                continue
            try:
                content = json.loads(stripped)
            except json.JSONDecodeError as exc:
                logger.warning("pi watcher: skipping malformed inbox line: {}", exc)
                continue
            if not isinstance(content, str):
                continue
            self._queue_tracker.enqueue(self._inbox_line_count, content, "")
            self._inbox_line_count += 1

    def _ingest_event(self, event: dict[str, Any]) -> None:
        """Add one parsed event: append a new id, supersede a changed one in place, drop an
        identical duplicate. A newly-appended ``user_message`` is a drained turn, so it pops
        one queue head (a supersede/duplicate never does)."""
        event_id = event["event_id"]
        existing_idx = self._event_index.get(event_id)
        if existing_idx is not None:
            if self._events[existing_idx] != event:
                self._events[existing_idx] = event
                if existing_idx < self._emitted_count:
                    self._superseded_pending[event_id] = event
            return
        self._event_index[event_id] = len(self._events)
        self._events.append(event)
        if event.get("type") == "user_message":
            self._queue_tracker.leave()

    def _adapt_line(self, line: str) -> list[dict[str, Any]]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            # The session file is pi-owned state, so an unparseable line is real corruption
            # rather than a shape to tolerate quietly: warn and skip so the rest still renders.
            logger.warning("pi watcher: skipping malformed session line: {}", exc)
            return []
        if not isinstance(record, dict):
            return []
        return parse_record(record)

    # --- read API (mirrors AgentSessionWatcher) ----------------------------
    # ``session_id`` is accepted for interface parity but inert: pi is one logical
    # session to the UI with no subagent sessions to filter.

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        self._refresh()
        with self._lock:
            return list(self._events)

    def get_tail_events(self, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        self._refresh()
        if limit <= 0:
            return []
        with self._lock:
            return list(self._events[-limit:])

    def get_backfill_events(
        self, before_event_id: str, limit: int = 50, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        self._refresh()
        if limit <= 0:
            return []
        with self._lock:
            idx = self._event_index.get(before_event_id)
            if idx is None:
                return []
            start = max(0, idx - limit)
            return list(self._events[start:idx])

    def get_forward_events(
        self, after_event_id: str, limit: int = 50, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        self._refresh()
        if limit <= 0:
            return []
        with self._lock:
            idx = self._event_index.get(after_event_id)
            if idx is None:
                return []
            return list(self._events[idx + 1 : idx + 1 + limit])

    def get_events_at_offset(self, offset: int, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        self._refresh()
        if limit <= 0:
            return []
        start = max(0, offset)
        with self._lock:
            return list(self._events[start : start + limit])

    def get_event_offset(self, event_id: str, session_id: str | None = None) -> int:
        self._refresh()
        with self._lock:
            idx = self._event_index.get(event_id)
            return idx if idx is not None else -1

    def get_total_event_count(self, session_id: str | None = None) -> int:
        self._refresh()
        with self._lock:
            return len(self._events)

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        """pi has no in-process subagents -- always None."""
        return None

    def is_main_session_event(self, event: dict[str, Any]) -> bool:
        """Every pi event belongs to the single main session."""
        return True

    # --- queued messages (the shoulder-tap surface) ------------------------

    def set_queue_snapshot_callback(self, callback: QueueSnapshotCallback) -> None:
        self._queue_snapshot_callback = callback

    def get_queued_messages(self) -> list[dict[str, Any]]:
        self._refresh()
        with self._lock:
            return list(self._queue_tracker.snapshot())

    def get_queued_block(self) -> str:
        self._refresh()
        with self._lock:
            return self._queue_tracker.concatenated_block()

    def clear_queue(self) -> None:
        """Drop the tracked queue (a flush restart handed the block back)."""
        with self._lock:
            self._queue_tracker.clear()
            snapshot = self._queue_tracker.snapshot()
        self._push_queue_snapshot(snapshot)

    def notify_idle(self) -> list[dict[str, Any]]:
        """Apply the working->IDLE backstop and return the resulting (empty) snapshot."""
        with self._lock:
            self._queue_tracker.on_idle()
            snapshot = self._queue_tracker.snapshot()
        # The backstop is an authoritative transition, so it settles the debounce window
        # immediately rather than waiting it out (the manager broadcasts the returned value).
        self._last_queue_snapshot = snapshot
        self._pending_queue_snapshot = snapshot
        self._pending_queue_since = self._now()
        return snapshot

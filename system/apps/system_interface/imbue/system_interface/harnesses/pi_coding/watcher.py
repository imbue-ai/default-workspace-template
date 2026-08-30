"""Tail a pi agent's native session JSONL and emit UI events.

Built on the shared :class:`~imbue.system_interface.harnesses.transcript_store` scaffolding
with a single lane: pi is one logical session to the UI. Unlike the earlier watcher --
which followed only the marker's live file, so everything before the last ``/new`` rotation
existed only in watcher memory and was forgotten on a backend restart -- this one registers
EVERY session file in the sessions dir: static (pre-rotation) files are consumed once in
chronological order, then the live file (followed across ``/new`` via the
``pi_session_file`` marker) is tailed incrementally. A rebuilt watcher therefore recovers
the full cross-rotation history from disk, which is what makes eviction-on-stop safe.

Queued messages (the shoulder tap) are populated from mngr's ``pi_inbox`` -- the file mngr
appends each outgoing message to before the extension injects it: each new inbox line is an
enqueue, each newly-committed ``user_message`` a leave. Both sides are scoped to the live
process generation: the lifecycle extension archives-and-truncates ``pi_inbox`` at load (so
the enqueue replay only ever sees current-generation lines), and leaves only pop when the
``user_message`` timestamp is at or after the ``pi_process_started`` marker's mtime (so a
dead generation's drains -- including everything in the static files -- never pop
current-generation entries).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger as _loguru_logger

from imbue.system_interface.activity_state import parse_iso_timestamp_to_epoch
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.pi_coding.inbox import is_sentinel_object
from imbue.system_interface.harnesses.pi_coding.queue_tracker import PiQueueTracker
from imbue.system_interface.harnesses.pi_coding.session_parser import parse_record
from imbue.system_interface.harnesses.pi_coding.session_parser import parse_record_detail
from imbue.system_interface.harnesses.session_watcher import OnEventsCallback
from imbue.system_interface.harnesses.session_watcher import QueueSnapshotCallback
from imbue.system_interface.harnesses.transcript_store import StoreBackedWatcher
from imbue.system_interface.harnesses.transcript_store import iter_line_spans
from imbue.system_interface.harnesses.transcript_store import split_at_last_complete_line

logger = _loguru_logger

# Marker holding the live native session file's absolute path (rewritten on session_start /
# session_switch, so it follows a /new rotation). Kept in sync with SESSION_FILE_NAME in
# mngr_pi_coding's plugin.py / lifecycle extension.
_MARKER_RELATIVE = Path("pi_session_file")
# The stable dir every native session file (across /new rotation) lives under -- what a
# recursive watch targets. Kept in sync with the plugin's PI_CODING_AGENT_DIR layout.
_SESSIONS_RELATIVE = Path("plugin") / "pi_coding" / "sessions"
# mngr's outgoing-message ledger (the enqueue source for the queue). Kept in sync with
# _INBOX_FILE_NAME in plugin.py.
_INBOX_RELATIVE = Path("pi_inbox")
# Process-start boundary marker, touched by mngr_pi_coding's launch prelude on every
# launch/resume. Kept in sync with _PROCESS_STARTED_MARKER_NAME in plugin.py /
# ``PiActivityTracker.marker_filename``.
_PROCESS_STARTED_MARKER_RELATIVE = Path("pi_process_started")

# The store's single lane: pi's whole timeline, across /new rotations.
_LANE = "main"


def read_marker_session_path(marker_path: Path) -> Path | None:
    """The absolute session-file path recorded in the marker, or None when absent/empty
    (the agent has not started a session yet)."""
    try:
        raw = marker_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return Path(raw) if raw else None


def _mtime_or_zero(path: Path) -> float:
    """A file's mtime, or 0.0 when it cannot be statted (sorts unreadable files first)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _is_current_generation_drain(event_timestamp: str | None, process_started_at: float | None) -> bool:
    """Whether a drained ``user_message`` belongs to the current pi process generation.

    ``process_started_at`` is the ``pi_process_started`` marker's mtime; a drain whose
    timestamp parses to before it was performed by a dead generation and must not pop a
    current-generation queue entry. Missing marker or an unparseable timestamp reads as
    current (pop): over-popping errs toward an empty mirror, the contract-safe direction.
    """
    if process_started_at is None:
        return True
    event_at = parse_iso_timestamp_to_epoch(event_timestamp)
    if event_at is None:
        return True
    return event_at >= process_started_at


class PiSessionWatcher(StoreBackedWatcher):
    """Watches a pi agent's native session files (+ inbox) and emits parsed UI events."""

    # Instance attributes declared at class level so a `build()` classmethod (no
    # __init__) can assign them while the type checker still resolves every access.
    _marker_path: Path
    _process_started_marker_path: Path
    _sessions_dir: Path
    _inbox_path: Path
    _current_path: Path | None
    _byte_offset: int
    _consumed_static_paths: set[Path]
    _is_static_scan_done: bool
    _queue_tracker: PiQueueTracker
    _inbox_offset: int
    _inbox_line_count: int
    _queue_snapshot_callback: QueueSnapshotCallback | None
    _last_queue_snapshot: list[dict[str, str]]

    @classmethod
    def build(cls, agent_info: AgentInfo, on_events: OnEventsCallback) -> "PiSessionWatcher":
        """Build from the agent record. pi needs only the state dir: its session files and
        inbox both live under it."""
        agent_state_dir = agent_info.agent_state_dir
        self = cls.__new__(cls)
        self._init_store_watcher(agent_info.id, on_events)
        self._marker_path = agent_state_dir / _MARKER_RELATIVE
        self._process_started_marker_path = agent_state_dir / _PROCESS_STARTED_MARKER_RELATIVE
        self._sessions_dir = agent_state_dir / _SESSIONS_RELATIVE
        self._inbox_path = agent_state_dir / _INBOX_RELATIVE

        # The live file being tailed and its cursor; static (pre-rotation) files are
        # consumed whole exactly once.
        self._current_path: Path | None = None
        self._byte_offset = 0
        self._consumed_static_paths: set[Path] = set()
        # Static files only ever appear through a rotation (the marker changing) or before
        # the first scan, so the rglob sweep is gated on those rather than run per refresh.
        self._is_static_scan_done = False

        # Queue: the populator, the inbox cursor, a monotonic line counter (for stable
        # queued ids), the snapshot sink, and the last snapshot pushed (so a cycle that
        # leaves the queue unchanged pushes nothing).
        self._queue_tracker = PiQueueTracker.build()
        self._inbox_offset = 0
        self._inbox_line_count = 0
        self._queue_snapshot_callback: QueueSnapshotCallback | None = None
        self._last_queue_snapshot: list[dict[str, str]] = []
        return self

    # -- base hooks -----------------------------------------------------------------------

    def _watch_paths(self) -> tuple[Path, ...]:
        # The sessions dir (recursive, so appends to whichever session file is live wake
        # the loop without re-scheduling on a /new rotation) plus the inbox file.
        return (self._sessions_dir, self._inbox_path)

    def _before_broadcast(self) -> None:
        # A3b depart-before-arrive: when a queued message drains into a real turn, the chip
        # must be removed before the transcript turn is shown -- push the snapshot (chip
        # removal) first, then the events (the turn).
        self._push_queue_snapshot_if_changed()

    def _refresh_locked(self) -> None:
        """Bring the store + queue up to date with disk: the inbox first (so an enqueue
        exists before the turn it drains into is counted as a leave), then the static
        session files (once each, in chronological order), then the live file."""
        self._consume_inbox_locked()
        process_started_at = self._read_process_started_at()
        target = read_marker_session_path(self._marker_path)

        # Static files: every session file that is not the live one, consumed whole once,
        # oldest first, so pre-rotation history lands in chronological order. This is what
        # lets a rebuilt watcher (backend restart, eviction) recover the full timeline.
        if not self._is_static_scan_done or target != self._current_path:
            try:
                candidates = list(self._sessions_dir.rglob("*.jsonl"))
            except OSError:
                candidates = []
            static_paths = [path for path in candidates if path != target and path not in self._consumed_static_paths]
            for path in sorted(static_paths, key=lambda p: (_mtime_or_zero(p), p.name)):
                self._consume_whole_file_locked(path, process_started_at)
                self._consumed_static_paths.add(path)
            self._is_static_scan_done = True

        if target is None:
            return
        if target != self._current_path:
            # First resolution or a /new rotation. Consume any remaining tail of the old
            # live file, then tail the new one from its start. pi's followUp queue does not
            # survive /new, so clear the queued set on a real rotation.
            if self._current_path is not None:
                self._consume_live_tail_locked(self._current_path, process_started_at)
                self._consumed_static_paths.add(self._current_path)
                self._queue_tracker.clear()
            self._current_path = target
            self._byte_offset = 0
        self._consume_live_tail_locked(target, process_started_at)

    # -- session-file consumption ---------------------------------------------------------

    def _consume_whole_file_locked(self, path: Path, process_started_at: float | None) -> None:
        try:
            raw = path.read_bytes()
        except OSError:
            logger.debug("pi watcher: failed to read {}", path)
            return
        self._ingest_spans_locked(path, iter_line_spans(raw, 0), process_started_at)

    def _consume_live_tail_locked(self, path: Path, process_started_at: float | None) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            # The marker points at a not-yet-created file; retry next cycle.
            return
        # Native session files are append-only; a shrink is unexpected -- re-read from the
        # start (the store's id-based dedup drops the re-read copies).
        if size < self._byte_offset:
            self._byte_offset = 0
        if size == self._byte_offset:
            return
        try:
            with path.open("rb") as f:
                f.seek(self._byte_offset)
                raw = f.read()
        except OSError:
            logger.debug("pi watcher: failed to read {}", path)
            return
        complete, _fragment = split_at_last_complete_line(raw)
        if not complete:
            return
        self._ingest_spans_locked(path, iter_line_spans(complete, self._byte_offset), process_started_at)
        self._byte_offset += len(complete)

    def _ingest_spans_locked(
        self, path: Path, spans: list[tuple[int, int, bytes]], process_started_at: float | None
    ) -> None:
        for byte_offset, byte_len, line_bytes in spans:
            stripped = line_bytes.decode("utf-8", errors="replace").strip()
            if not stripped:
                continue
            for event in self._adapt_line(stripped):
                is_new = self._store.ingest(_LANE, event, (path, byte_offset, byte_len))
                # A newly-committed ``user_message`` from the current process generation is
                # a drained turn, so it pops one queue head; a dead generation's drain (and
                # any supersede/duplicate) leaves the queue untouched.
                if (
                    is_new
                    and event.get("type") == "user_message"
                    and _is_current_generation_drain(event.get("timestamp"), process_started_at)
                ):
                    self._queue_tracker.leave()

    def _adapt_line(self, line: str) -> list[dict[str, Any]]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            # The session file is pi-owned state, so an unparseable line is real corruption
            # rather than a shape to tolerate quietly: warn and skip so the rest renders.
            logger.warning("pi watcher: skipping malformed session line: {}", exc)
            return []
        if not isinstance(record, dict):
            return []
        return parse_record(record)

    # -- inbox / queue --------------------------------------------------------------------

    def _consume_inbox_locked(self) -> None:
        """Read new lines from ``pi_inbox`` and enqueue each into the queue populator.

        The inbox is append-only and mngr-owned; a shrink (unexpected) re-reads from the
        start, and the queue set is rebuilt from position on the next leaves. A
        flush/retract sentinel object line clears the tracked queue at its replay position:
        every message before it was committed (flush) or discarded (retract) by the
        extension, so clearing at the sentinel keeps the ledger balanced across backend
        restarts.
        """
        path = self._inbox_path
        try:
            size = path.stat().st_size
        except OSError:
            # No inbox yet (nothing sent): nothing to do.
            return
        if size < self._inbox_offset:
            self._inbox_offset = 0
            self._inbox_line_count = 0
            self._queue_tracker.reset()
        if size == self._inbox_offset:
            return
        try:
            with path.open("rb") as f:
                f.seek(self._inbox_offset)
                raw = f.read()
        except OSError:
            logger.debug("pi watcher: failed to read inbox {}", path)
            return
        complete, _fragment = split_at_last_complete_line(raw)
        if not complete:
            return
        self._inbox_offset += len(complete)
        for byte_line in complete.split(b"\n"):
            stripped = byte_line.decode("utf-8", errors="replace").strip()
            if not stripped:
                continue
            try:
                content = json.loads(stripped)
            except json.JSONDecodeError as exc:
                logger.warning("pi watcher: skipping malformed inbox line: {}", exc)
                continue
            if is_sentinel_object(content):
                # A shoulder-tap flush or stop retract: everything queued before it left
                # the live session, so drop the tracked queue positionally.
                self._queue_tracker.clear()
                continue
            if not isinstance(content, str):
                continue
            self._queue_tracker.enqueue(self._inbox_line_count, content, "")
            self._inbox_line_count += 1

    def _read_process_started_at(self) -> float | None:
        """The ``pi_process_started`` marker's mtime, or None when it is absent."""
        try:
            return self._process_started_marker_path.stat().st_mtime
        except OSError:
            return None

    def _push_queue_snapshot_if_changed(self) -> None:
        """Push the live queued snapshot to the sink iff it differs from the last pushed.

        No debounce: a queued chip surfaces as soon as the message is parked and clears as
        soon as it drains, mirroring pi's real inbox state (contract A3: the UI queue IS
        the harness queue).
        """
        callback = self._queue_snapshot_callback
        with self._lock:
            snapshot = self._queue_tracker.snapshot()
            if snapshot == self._last_queue_snapshot:
                return
            self._last_queue_snapshot = snapshot
        if callback is not None:
            callback(snapshot)

    # -- queued messages (the shoulder-tap surface) ---------------------------------------

    def set_queue_snapshot_callback(self, callback: QueueSnapshotCallback) -> None:
        self._queue_snapshot_callback = callback

    def get_queued_messages(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_locked()
            return list(self._queue_tracker.snapshot())

    def get_queued_block(self) -> str:
        with self._lock:
            self._refresh_locked()
            return self._queue_tracker.concatenated_block()

    def clear_queue(self) -> None:
        """Drop the tracked queue (a flush restart handed the block back)."""
        with self._lock:
            self._queue_tracker.clear()
        self._push_queue_snapshot_if_changed()

    def notify_idle(self) -> list[dict[str, Any]]:
        """Apply the working->IDLE backstop and return the resulting (empty) snapshot."""
        with self._lock:
            self._queue_tracker.on_idle()
            snapshot = self._queue_tracker.snapshot()
            # Record it as the last-pushed snapshot so the subsequent poll does not re-push
            # the same empty set (the manager broadcasts the returned value directly).
            self._last_queue_snapshot = snapshot
        return snapshot

    # -- on-demand payload detail ---------------------------------------------------------

    def _parse_detail(
        self, event: dict[str, Any], source_line: str | None, thinking_line: str | None
    ) -> dict[str, Any] | None:
        if source_line is None:
            return None
        try:
            record = json.loads(source_line.strip())
        except json.JSONDecodeError as e:
            # A recorded byte range no longer decodes: a stale range or real corruption,
            # rare either way and worth surfacing (the fallback scan runs next).
            logger.warning("pi watcher: undecodable session line during a payload read: {}", e)
            return None
        if not isinstance(record, dict):
            return None
        return parse_record_detail(record).get(event["event_id"])

    def _find_detail_source_fallback(self, event: dict[str, Any]) -> str | None:
        """Scan the live session file for the record that carries this event's payloads."""
        with self._lock:
            target = self._current_path
        if target is None:
            return None
        event_id = event["event_id"]
        try:
            with target.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        record = json.loads(stripped)
                    except json.JSONDecodeError as e:
                        # A complete line failing to decode mid-scan is corruption; the
                        # trailing partial line (mid-write) is the routine near-miss.
                        logger.warning("pi watcher: undecodable line in a payload fallback scan: {}", e)
                        continue
                    if isinstance(record, dict) and event_id in parse_record_detail(record):
                        return line
        except OSError:
            return None
        return None

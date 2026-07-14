"""Tail a codex agent's pre-converted common-transcript and emit UI events.

The codex analogue of :class:`session_watcher.AgentSessionWatcher`, but far
simpler. mngr_codex runs an agent-side converter that already normalizes codex's
native rollout into mngr's harness-agnostic common-transcript schema, appended to a
single file:

    <agent_state_dir>/events/codex/common_transcript/events.jsonl

So this watcher does not parse codex's native format, walk a ``projects/`` tree, or
track subagent sessions (codex's common transcript has no subagent linkage). It just
tails that one append-only file, adapts each record to the UI event schema via
:func:`codex_session_parser.adapt_common_transcript_record`, dedups by ``event_id``,
and fans new events out through ``on_events`` -- the same callback contract
``AgentSessionWatcher`` uses, so :mod:`app_context`'s broadcast/SSE plumbing is
unchanged.

It exposes the same read/pagination API the server calls on a watcher, backed by a
simple in-memory ordered list. codex has a single logical session from the UI's
point of view, so the ``session_id`` parameter these methods accept is inert (there
are no subagent sessions to filter) and :meth:`get_subagent_metadata` always returns
``None``.

Watching is poll-based (``POLL_INTERVAL_SECONDS``), matching the cadence the claude
watcher's watchdog falls back to and the cadence ``mngr event --follow`` itself uses.
A watchdog observer is intentionally omitted for this first cut: the transcript
directory does not exist until the agent's first turn, which complicates scheduling
an observer, and a 1s poll is well within the latency budget for a chat transcript.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from typing import Callable

from loguru import logger as _loguru_logger

from imbue.system_interface.codex_session_parser import adapt_common_transcript_record
from imbue.system_interface.watcher_common import POLL_INTERVAL_SECONDS

logger = _loguru_logger

# Relative location of the codex common-transcript under an agent's state dir.
# Mirrors mngr_codex.codex_config.COMMON_TRANSCRIPT_OUTPUT_RELATIVE; kept as a local
# constant (rather than importing the plugin) so the web backend does not couple its
# import graph to plugin internals -- the same reimplement-don't-import stance
# session_parser takes toward mngr_claude's converter.
_COMMON_TRANSCRIPT_RELATIVE = Path("events") / "codex" / "common_transcript" / "events.jsonl"


class CodexSessionWatcher:
    """Watches a codex agent's common-transcript file and emits parsed UI events."""

    def __init__(
        self,
        agent_id: str,
        agent_state_dir: Path,
        on_events: Callable[[str, list[dict[str, Any]]], None],
    ) -> None:
        self._agent_id = agent_id
        self._transcript_path = agent_state_dir / _COMMON_TRANSCRIPT_RELATIVE
        self._on_events = on_events

        # Guards the in-memory transcript mirror and the tail cursor. Held across
        # the (cheap, incremental) file read + adapt, but never across the
        # ``on_events`` fan-out callback -- the same discipline AgentSessionWatcher
        # follows.
        self._lock = threading.Lock()
        # Adapted UI events, in append (chronological) order.
        self._events: list[dict[str, Any]] = []
        # event_id -> index into _events, for O(1) offset lookup + dedup.
        self._event_index: dict[str, int] = {}
        # Bytes of the transcript file already consumed.
        self._byte_offset = 0
        # A trailing partial line (no newline yet) carried to the next read.
        self._partial = ""

        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start tailing the transcript in a background thread."""
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"codex-watcher-{self._agent_id}")
        self._thread.start()

    def stop(self) -> None:
        """Stop tailing."""
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    # --- background loop ---------------------------------------------------

    def _run(self) -> None:
        # Emit whatever already exists on first read (agent may have run before the
        # UI connected), then poll for appended lines.
        self._emit(self._consume_new_lines())
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=POLL_INTERVAL_SECONDS)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            self._emit(self._consume_new_lines())

    def _emit(self, events: list[dict[str, Any]]) -> None:
        if events:
            self._on_events(self._agent_id, events)

    def _consume_new_lines(self) -> list[dict[str, Any]]:
        """Read bytes appended since the last cursor, adapt complete lines, return new events."""
        path = self._transcript_path
        try:
            size = path.stat().st_size
        except OSError:
            # File not created yet (agent hasn't produced a transcript) -- normal.
            return []

        new_events: list[dict[str, Any]] = []
        with self._lock:
            # Truncation / rotation: the file shrank, so our cursor is stale. Reset
            # and re-read from the start. The converter only ever appends, so this
            # is defensive, not expected.
            if size < self._byte_offset:
                self._events.clear()
                self._event_index.clear()
                self._byte_offset = 0
                self._partial = ""
            if size == self._byte_offset and not self._partial:
                return []

            try:
                with path.open("rb") as f:
                    f.seek(self._byte_offset)
                    raw = f.read()
            except OSError:
                logger.debug("codex watcher: failed to read {}", path)
                return []
            self._byte_offset += len(raw)

            data = self._partial + raw.decode("utf-8", errors="replace")
            lines = data.split("\n")
            # The final element is the trailing (possibly empty) partial line; carry
            # it forward so a half-written record is completed on the next read.
            self._partial = lines.pop()

            for line in lines:
                line = line.strip()
                if not line:
                    continue
                event = self._adapt_line(line)
                if event is None:
                    continue
                event_id = event["event_id"]
                if event_id in self._event_index:
                    continue
                self._event_index[event_id] = len(self._events)
                self._events.append(event)
                new_events.append(event)

        return new_events

    def _adapt_line(self, line: str) -> dict[str, Any] | None:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("codex watcher: skipping malformed transcript line")
            return None
        if not isinstance(record, dict):
            return None
        return adapt_common_transcript_record(record)

    # --- read API (mirrors AgentSessionWatcher) ----------------------------
    #
    # ``session_id`` is accepted for interface parity with AgentSessionWatcher but
    # is inert: codex's common transcript is a single logical session with no
    # subagent sessions to filter.

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Return every parsed event in chronological order."""
        with self._lock:
            return list(self._events)

    def get_tail_events(self, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        """Return the most recent ``limit`` events (chronological order)."""
        if limit <= 0:
            return []
        with self._lock:
            return list(self._events[-limit:])

    def get_backfill_events(
        self, before_event_id: str, limit: int = 50, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` events immediately before ``before_event_id``."""
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
        """Return up to ``limit`` events immediately after ``after_event_id``."""
        if limit <= 0:
            return []
        with self._lock:
            idx = self._event_index.get(after_event_id)
            if idx is None:
                return []
            return list(self._events[idx + 1 : idx + 1 + limit])

    def get_events_at_offset(self, offset: int, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        """Return up to ``limit`` events starting at global index ``offset`` (clamped)."""
        if limit <= 0:
            return []
        start = max(0, offset)
        with self._lock:
            return list(self._events[start : start + limit])

    def get_event_offset(self, event_id: str, session_id: str | None = None) -> int:
        """Global index of ``event_id``, or -1 if unknown."""
        with self._lock:
            idx = self._event_index.get(event_id)
            return idx if idx is not None else -1

    def get_total_event_count(self, session_id: str | None = None) -> int:
        """Total number of events in the transcript."""
        with self._lock:
            return len(self._events)

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        """codex has no subagent linkage in the common transcript -- always None."""
        return None

    def is_main_session_event(self, event: dict[str, Any]) -> bool:
        """Every codex event belongs to the single main session."""
        return True

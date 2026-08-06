"""Tail a codex agent's live rollout and emit UI events.

The codex analogue of :class:`claude_session_watcher.ClaudeSessionWatcher`. It tails
codex's OWN on-disk rollout in real time -- the same file codex writes as it works --
parses each line to the UI event schema via
:func:`codex_session_parser.parse_lines`, dedups by ``event_id``, and
fans new events out through ``on_events`` (the same callback contract
``ClaudeSessionWatcher`` uses, so :mod:`app_context`'s broadcast/SSE plumbing is
unchanged). It reads the live file -- not mngr_codex's stream_transcript.sh mirror --
because the mirror lags codex by up to its 1s poll, long enough for the optimistic
"sending" bubble to visibly flip to "queued" before it reconciles. Reading the live
file directly is how the claude watcher already works.

Which rollout is live rotates (a fresh file per session, and again on resume), so --
like claude following its ``claude_session_id_history`` -- we follow the active file
via a marker: mngr_codex writes its absolute path to
``<agent_state_dir>/codex_transcript_path`` every turn. Each cycle we re-read that
marker; when it points somewhere new we switch files (from the new file's start),
keeping the global line counter, tool-name map, and accumulated events/dedup so a
resume's re-serialised history (same codex ``id``s) dedups against what we already
emitted. The watchdog is a recursive observer on the stable
``<agent_state_dir>/plugin/codex/home/sessions`` dir (all rollouts live under it), so
appends -- to whichever rollout is live -- wake the loop immediately, with the 1s poll
as a safety net.

Simpler than the claude watcher in the parse layer: no two-tier cache (the parser
reads incrementally in order and never reparses a single line, so a plain in-memory
list + stable event ids suffice), and (this first cut) no subagent-session tracking.
It exposes the same read/pagination API the server calls; ``session_id`` on those
methods is inert (codex is one logical session to the UI) and
:meth:`get_subagent_metadata` always returns ``None``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from typing import Callable

from loguru import logger as _loguru_logger

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.events import SPECIAL_EVENT_TYPE
from imbue.system_interface.harnesses.events import SpecialEventKind
from imbue.system_interface.harnesses.path_watch import PathWatcher
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher
from imbue.system_interface.harnesses.session_watcher import OnEventsCallback
from imbue.system_interface.harnesses.codex.session_parser import SOURCE as _SOURCE
from imbue.system_interface.harnesses.codex.session_parser import normalize_user_content
from imbue.system_interface.harnesses.codex.session_parser import parse_lines
from imbue.system_interface.harnesses.codex.session_parser import queued_input_event

logger = _loguru_logger

# We tail codex's LIVE rollout directly (the same real-time file codex writes),
# not the stream_transcript.sh mirror -- the mirror lags codex by up to its 1s poll,
# long enough for the "sending" bubble to visibly flip to "queued" before it
# reconciles. Reading the live file is the codex analogue of how the claude watcher
# reads claude's own on-disk transcript directly.
#
# Which file is live rotates (a new rollout per session, and again on resume), so
# like claude (claude_session_id_history), we follow it via a marker: mngr_codex
# writes the active rollout's absolute path to <agent_state_dir>/codex_transcript_path
# on every turn. All rollouts live under <agent_state_dir>/plugin/codex/home/sessions,
# which we watchdog recursively (stable path, catches every rollout's appends without
# re-scheduling on rotation). Constants kept local (not imported from the plugin),
# mirroring claude_session_parser's reimplement-don't-import stance.
_MARKER_RELATIVE = Path("codex_transcript_path")
_SESSIONS_RELATIVE = Path("plugin") / "codex" / "home" / "sessions"
# The queued-input sidecar the patched codex binary appends to on every enqueue, a
# sibling of ``sessions/`` under CODEX_HOME. Watched alongside the rollout so a message
# queued mid-turn surfaces as a bubble immediately, rather than only when it drains into
# the rollout at the end of the running turn.
_QUEUED_INPUT_RELATIVE = Path("plugin") / "codex" / "home" / "queued_input.jsonl"


def read_marker_rollout_path(marker_path: Path) -> Path | None:
    """The absolute rollout path recorded in a codex marker file, or None when the
    marker is absent/empty (the agent has not taken a turn yet)."""
    try:
        raw = marker_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return Path(raw) if raw else None


def resolve_active_rollout_path(agent_state_dir: Path) -> Path | None:
    """The live rollout for a codex agent, per its marker. Shared by the watcher and
    the model resolver so the marker-read lives in one place (they keep separate read
    cursors over the file itself, by design)."""
    return read_marker_rollout_path(agent_state_dir / _MARKER_RELATIVE)


def codex_sessions_dir(agent_state_dir: Path) -> Path:
    """The stable dir every rollout (across rotation) lives under -- what a recursive
    watch targets."""
    return agent_state_dir / _SESSIONS_RELATIVE


class CodexSessionWatcher(AgentSessionWatcher):
    """Watches a codex agent's raw rollout file and emits parsed UI events."""

    # Instance attributes declared at class level so a `build()` classmethod (no
    # __init__) can assign them while the type checker still resolves every access.
    _agent_id: str
    _marker_path: Path
    _sessions_dir: Path
    _on_events: Callable[[str, list[dict[str, Any]]], None]
    _lock: threading.Lock
    _events: list[dict[str, Any]]
    _event_index: dict[str, int]
    # Already-emitted events that a later rollout line superseded (same id, new
    # content), keyed by id so repeated supersessions collapse to the latest. Drained
    # by _emit_unsent, which re-broadcasts them so the client upgrades the held copy in
    # place. A supersession of a not-yet-emitted event needs no entry -- the normal tail
    # broadcast already carries its latest content.
    _superseded_pending: dict[str, dict[str, Any]]
    _current_path: Path | None
    _byte_offset: int
    _emitted_count: int
    # A trailing partial line carried across reads as RAW BYTES (not str): a multi-byte
    # UTF-8 character split across a read boundary must be completed before decoding, or
    # decoding the fragment corrupts/drops the character.
    _partial: bytes
    _line_index: int
    _tool_name_by_call_id: dict[str, str]
    # The queued-input sidecar and its own byte cursor + trailing-partial, read exactly
    # like the rollout but from a separate file. ``_queued_id_by_content`` maps a queued
    # message's normalised content to the placeholder event id it was emitted under, so
    # the real rollout turn (which carries no queued_id) supersedes the placeholder in
    # place instead of double-rendering. Kept for the whole session -- never popped -- so
    # a resume that re-serialises the drained turn still dedups against it.
    _queued_input_path: Path
    _queued_offset: int
    _queued_partial: bytes
    _queued_id_by_content: dict[str, str]
    # The shared watch loop: a recursive watchdog on the sessions dir plus the poll
    # safety net, invoking _emit_unsent once at start and on every wake. Replaces the
    # bespoke thread/observer/poll block this watcher used to hand-roll.
    _path_watcher: PathWatcher | None

    @classmethod
    def build(cls, agent_info: AgentInfo, on_events: OnEventsCallback) -> "CodexSessionWatcher":
        """Build from the agent record. Codex needs only the state dir: its rollout lives
        under the per-agent CODEX_HOME there, so ``claude_config_dir`` is never read."""
        agent_state_dir = agent_info.agent_state_dir
        self = cls.__new__(cls)
        self._agent_id = agent_info.id
        # Marker file holding the active rollout's absolute path (rewritten each turn,
        # so it follows rotation), and the sessions dir we watchdog.
        self._marker_path = agent_state_dir / _MARKER_RELATIVE
        self._sessions_dir = codex_sessions_dir(agent_state_dir)
        self._queued_input_path = agent_state_dir / _QUEUED_INPUT_RELATIVE
        self._on_events = on_events

        # Guards the in-memory transcript mirror and the tail cursor. Held across
        # the (cheap, incremental) file read + adapt, but never across the
        # ``on_events`` fan-out callback -- the same discipline ClaudeSessionWatcher
        # follows.
        self._lock = threading.Lock()
        # Adapted UI events, in append (chronological) order.
        self._events = []
        # event_id -> index into _events, for O(1) offset lookup + dedup/supersede.
        self._event_index = {}
        # Already-emitted events a later line superseded, awaiting re-broadcast.
        self._superseded_pending = {}
        # The rollout file currently being tailed (resolved from the marker); None
        # until the first turn writes the marker. Rotation = marker points elsewhere.
        self._current_path = None
        # Bytes of _current_path already consumed; reset only on rotation / re-read.
        # This is a READ cursor: reads advance it too (see ``_refresh``), so it says
        # nothing about what subscribers have seen.
        self._byte_offset = 0
        # How many of _events have been broadcast via ``on_events``. Separate from the
        # read cursor precisely so a read may advance the latter without the background
        # thread then skipping those events -- ClaudeSessionWatcher keeps the same split,
        # which is what lets its read paths refresh from disk on every call.
        self._emitted_count = 0
        # A trailing partial line (no newline yet) carried to the next read, as bytes.
        self._partial = b""
        # GLOBAL monotonic line counter for synthetic event ids (event_msg user_message
        # has no codex id). Never reset -- keeps ids unique ACROSS rollout files so a
        # resume's line 5 can't collide with the prior file's line 5. (id-based events
        # use codex's own msg id / call_id, so they dedup re-serialised copies
        # regardless.)
        self._line_index = 0
        # call_id -> tool_name, so a function_call_output can recover its tool name
        # from the earlier function_call. Persists across files (a resume re-serialises
        # the calls, but keeping the map is harmless and covers output-only cases).
        self._tool_name_by_call_id = {}
        # Queued-input sidecar cursor + dedup map (see the attribute docs above).
        self._queued_offset = 0
        self._queued_partial = b""
        self._queued_id_by_content = {}

        self._path_watcher = None
        return self

    def start(self) -> None:
        """Start tailing the transcript in a background thread.

        The watch loop is the shared :class:`PathWatcher` on the stable sessions dir
        (watched recursively, so appends to whichever rollout is live wake it without
        re-scheduling on rotation). It calls ``_emit_unsent`` once at start -- to
        broadcast whatever already exists, since the agent may have run before the UI
        connected -- and again on every filesystem wake or poll timeout.
        """
        # Watch CODEX_HOME (the parent of ``sessions/``) recursively: this catches every
        # rollout append as before AND the queued-input sidecar's appends, both of which
        # feed the transcript. The sidecar is a sibling of ``sessions/``, not under it, so
        # watching only the sessions dir would miss it.
        self._path_watcher = PathWatcher.build((self._sessions_dir.parent,), self._emit_unsent)
        self._path_watcher.start()

    def stop(self) -> None:
        """Stop tailing."""
        if self._path_watcher is not None:
            self._path_watcher.stop()

    def _refresh(self) -> None:
        """Bring the in-memory transcript up to date with the rollout on disk.

        Called by the background loop AND by every read method, mirroring the
        ``_discover_sessions()`` call at the top of ClaudeSessionWatcher's read paths:
        a read must never depend on the loop having run, or the first request after a
        restart answers "no history" for a transcript that is sitting on disk -- and the
        client caches that answer. Incremental, so a caught-up refresh reads no bytes.
        """
        self._consume_new_lines()

    def _emit_unsent(self) -> None:
        """Refresh, then broadcast every event not yet sent to subscribers.

        Keyed off ``_emitted_count`` rather than off what this read happened to parse,
        so events a *reader* pulled in are still delivered exactly once.
        """
        self._refresh()
        with self._lock:
            # New tail events (delivered once), plus any already-emitted events a line
            # superseded since the last emit -- re-broadcast so the client upgrades its
            # held copy in place (order does not matter: the client keys the upgrade on
            # event_id, not position).
            pending = self._superseded_pending
            self._superseded_pending = {}
            to_send = list(pending.values()) + self._events[self._emitted_count :]
            self._emitted_count = len(self._events)
        if to_send:
            self._on_events(self._agent_id, to_send)

    def _read_active_rollout(self) -> Path | None:
        """The absolute path of the live rollout, per the marker; None until written."""
        return read_marker_rollout_path(self._marker_path)

    def _consume_new_lines(self) -> list[dict[str, Any]]:
        """Read bytes appended to the live rollout since the last cursor, following
        rotation (a new rollout on resume) via the marker."""
        target = self._read_active_rollout()
        if target is None:
            return []

        new_events: list[dict[str, Any]] = []
        with self._lock:
            # Read the queued-input sidecar first, so a message's placeholder exists before
            # the rollout turn it later drains into is deduped against it.
            self._consume_queued_input(new_events)
            if target != self._current_path:
                # First resolution or rotation (resume -> new rollout). Tail the new
                # file from its start. Keep _line_index (global -> ids stay unique
                # across files), _tool_name_by_call_id, and _events/_event_index so a
                # resume's re-serialised history (same codex msg ids) dedups against
                # what we already emitted and the accumulated transcript survives.
                self._current_path = target
                self._byte_offset = 0
                self._partial = b""

            try:
                size = target.stat().st_size
            except OSError:
                # marker points at a not-yet-created file; retry next cycle
                return []

            # Codex rollouts are append-only; a shrink is unexpected. Re-read from the
            # start -- id-based dedup drops the re-emitted assistant/tool events.
            if size < self._byte_offset:
                self._byte_offset = 0
                self._partial = b""
            if size == self._byte_offset and not self._partial:
                return []

            try:
                with target.open("rb") as f:
                    f.seek(self._byte_offset)
                    raw = f.read()
            except OSError:
                logger.debug("codex watcher: failed to read {}", target)
                return []
            self._byte_offset += len(raw)

            # Split on the newline BYTE and carry the trailing partial forward as bytes,
            # then decode each COMPLETE line: a `\n` never falls inside a UTF-8 character,
            # so a whole line always decodes cleanly, and a character split across the read
            # boundary is completed (as bytes) before it is ever decoded.
            byte_lines = (self._partial + raw).split(b"\n")
            self._partial = byte_lines.pop()

            for byte_line in byte_lines:
                # Every physical line consumes an index (even blanks/skips) so a
                # given line always maps to the same id across the run.
                idx = self._line_index
                self._line_index += 1
                stripped = byte_line.decode("utf-8", errors="replace").strip()
                if not stripped:
                    continue
                for event in self._adapt_line(stripped, idx):
                    # If this rollout turn is the drained form of a still-queued message,
                    # re-key it onto that placeholder's id so it supersedes it in place.
                    self._dedup_queued_turn(event)
                    self._ingest_event(event, new_events)
                    # A user interrupt (turn_aborted) leaves any in-flight tool call with
                    # no result -- codex never persists one -- so its card would spin
                    # forever. Synthesise a terminal "Interrupted." result for every open
                    # call, keyed on the id a real result would use so a real one (if codex
                    # ever writes it) supersedes this via the same path.
                    if event.get("type") == SPECIAL_EVENT_TYPE and event.get("kind") == SpecialEventKind.TURN_ABORTED:
                        for synthetic in self._interrupt_results(event.get("timestamp", "")):
                            self._ingest_event(synthetic, new_events)

        return new_events

    def _consume_queued_input(self, new_events: list[dict[str, Any]]) -> None:
        """Read new lines from the queued-input sidecar and ingest each as a placeholder
        user bubble. Records content -> placeholder-id so the drained rollout turn
        supersedes it. Must hold ``_lock``.

        The sidecar is append-only and TUI-owned (never rotated/truncated by core), so the
        cursor only ever moves forward; a shrink (unexpected) re-reads from the start, and
        id-based dedup collapses any re-emitted placeholder.
        """
        path = self._queued_input_path
        try:
            size = path.stat().st_size
        except OSError:
            # No sidecar yet (agent has queued nothing, or an older binary): nothing to do.
            return
        if size < self._queued_offset:
            self._queued_offset = 0
            self._queued_partial = b""
        if size == self._queued_offset and not self._queued_partial:
            return
        try:
            with path.open("rb") as f:
                f.seek(self._queued_offset)
                raw = f.read()
        except OSError:
            logger.debug("codex watcher: failed to read queued-input sidecar {}", path)
            return
        self._queued_offset += len(raw)
        byte_lines = (self._queued_partial + raw).split(b"\n")
        self._queued_partial = byte_lines.pop()
        for byte_line in byte_lines:
            stripped = byte_line.decode("utf-8", errors="replace").strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as exc:
                logger.warning("codex watcher: skipping malformed queued-input line: {}", exc)
                continue
            if not isinstance(record, dict):
                continue
            event = queued_input_event(record)
            if event is None:
                continue
            self._queued_id_by_content[normalize_user_content(event["content"])] = event["event_id"]
            self._ingest_event(event, new_events)

    def _dedup_queued_turn(self, event: dict[str, Any]) -> None:
        """Re-key a rollout ``user_message`` onto its queued placeholder's id when it is the
        drained form of a message we already showed as queued.

        The drained turn carries no ``queued_id``, so the match is by normalised content --
        the same brittleness the frontend's content reconcile has, and the reason the
        placeholder uses codex's ``queued_id`` in the first place. Mutating the id in place
        lets ``_ingest_event`` supersede the placeholder rather than append a second bubble.
        """
        if event.get("type") != "user_message":
            return
        content = event.get("content")
        if not isinstance(content, str):
            return
        placeholder_id = self._queued_id_by_content.get(normalize_user_content(content))
        if placeholder_id is not None and placeholder_id != event["event_id"]:
            event["event_id"] = placeholder_id
            event["message_uuid"] = placeholder_id

    def _ingest_event(self, event: dict[str, Any], new_events: list[dict[str, Any]]) -> None:
        """Add one parsed event to the view: append a new id, supersede a changed one in
        place (re-broadcasting an already-emitted change), drop an identical duplicate."""
        event_id = event["event_id"]
        existing_idx = self._event_index.get(event_id)
        if existing_idx is not None:
            # Supersession: codex re-serialises an event (same id) with updated content --
            # replace the stored copy in place so the view holds the latest, not the stale
            # first. An identical re-serialisation is a pure duplicate and is dropped. A
            # re-broadcast lets the client upgrade its held copy; only an ALREADY-EMITTED
            # supersession needs one (a not-yet-emitted event is carried latest by the tail).
            if self._events[existing_idx] != event:
                self._events[existing_idx] = event
                new_events.append(event)
                if existing_idx < self._emitted_count:
                    self._superseded_pending[event_id] = event
            return
        self._event_index[event_id] = len(self._events)
        self._events.append(event)
        new_events.append(event)

    def _interrupt_results(self, timestamp: str) -> list[dict[str, Any]]:
        """Synthetic terminal tool_results for every tool call still open (no result) in
        the accumulated view -- what an interrupt leaves behind. Each is keyed on the same
        ``codex-result-<call_id>`` id a real result would carry, so a real result written
        later supersedes it rather than duplicating."""
        matched = {e.get("tool_call_id") for e in self._events if e.get("type") == "tool_result"}
        results: list[dict[str, Any]] = []
        for event in self._events:
            if event.get("type") != "assistant_message":
                continue
            for tool_call in event.get("tool_calls") or ():
                call_id = tool_call.get("tool_call_id")
                if not call_id or call_id in matched:
                    continue
                # Record it so a call appearing twice yields a single synthetic.
                matched.add(call_id)
                results.append(
                    {
                        "timestamp": timestamp,
                        "type": "tool_result",
                        "event_id": f"codex-result-{call_id}",
                        "source": _SOURCE,
                        "tool_call_id": call_id,
                        "tool_name": self._tool_name_by_call_id.get(call_id, ""),
                        "output": "Interrupted.",
                        "is_error": True,
                        "message_uuid": f"codex-result-{call_id}",
                    }
                )
        return results

    def _adapt_line(self, line: str, line_index: int) -> list[dict[str, Any]]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            # The rollout is codex-owned state, so a line we cannot parse is real
            # corruption rather than a shape we should tolerate quietly: warn so it is
            # visible, and skip the line so the rest of the transcript still renders.
            logger.warning("codex watcher: skipping malformed rollout line {}: {}", line_index, exc)
            return []
        if not isinstance(record, dict):
            return []
        return parse_lines(record, line_index, self._tool_name_by_call_id)

    # --- read API (mirrors AgentSessionWatcher) ----------------------------
    #
    # ``session_id`` is accepted for interface parity with AgentSessionWatcher but
    # is inert: codex's common transcript is a single logical session with no
    # subagent sessions to filter.

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Return every parsed event in chronological order."""
        self._refresh()
        with self._lock:
            return list(self._events)

    def get_tail_events(self, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        """Return the most recent ``limit`` events (chronological order)."""
        self._refresh()
        if limit <= 0:
            return []
        with self._lock:
            return list(self._events[-limit:])

    def get_backfill_events(
        self, before_event_id: str, limit: int = 50, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` events immediately before ``before_event_id``."""
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
        """Return up to ``limit`` events immediately after ``after_event_id``."""
        self._refresh()
        if limit <= 0:
            return []
        with self._lock:
            idx = self._event_index.get(after_event_id)
            if idx is None:
                return []
            return list(self._events[idx + 1 : idx + 1 + limit])

    def get_events_at_offset(self, offset: int, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        """Return up to ``limit`` events starting at global index ``offset`` (clamped)."""
        self._refresh()
        if limit <= 0:
            return []
        start = max(0, offset)
        with self._lock:
            return list(self._events[start : start + limit])

    def get_event_offset(self, event_id: str, session_id: str | None = None) -> int:
        """Global index of ``event_id``, or -1 if unknown."""
        self._refresh()
        with self._lock:
            idx = self._event_index.get(event_id)
            return idx if idx is not None else -1

    def get_total_event_count(self, session_id: str | None = None) -> int:
        """Total number of events in the transcript."""
        self._refresh()
        with self._lock:
            return len(self._events)

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        """codex has no subagent linkage in the common transcript -- always None."""
        return None

    def is_main_session_event(self, event: dict[str, Any]) -> bool:
        """Every codex event belongs to the single main session."""
        return True

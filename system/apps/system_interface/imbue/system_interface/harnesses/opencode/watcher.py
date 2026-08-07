"""Tail an opencode agent's own ``opencode.db`` and emit UI events.

The opencode analogue of :class:`codex.watcher.CodexSessionWatcher`, but reading opencode's
OWN SQLite conversation store rather than a JSONL rollout. opencode keeps its whole conversation
in ``<agent_state_dir>/plugin/opencode/data/opencode/opencode.db`` (WAL mode); this watcher
tails that db directly -- the single source of truth the resume/adopt paths already read -- so
it needs neither the plugin's raw-transcript mirror nor its idle-latched common transcript.

Unlike pi's append-only JSONL, opencode UPDATES ``message``/``part`` rows in place as a turn
streams (a text part's text grows, a tool part goes running -> completed, the assistant message
gains its ``finish``). So the tail cannot be a byte offset; it copies **antigravity** (which
tails a live SQLite store the same way): a ``time_updated`` watermark that re-scans the hot
tail every poll and advances only past *settled* messages, with codex-style content-supersession
dedup on stable ``msg_``/``prt_`` event ids -- so an in-place update supersedes the already-shown
event in place rather than duplicating it.

Subagents are disabled, so the transcript is the single ROOT session (resolved from the
``opencode_root_session`` marker); there is no ``parent_id`` walk. opencode delivers a message
by writing it straight into ``message`` (no parked-queue ledger -- verified), so there is no
queued-message populator: the base no-op queue methods are inherited, and a message sent while
busy surfaces as an ordinary ``user_message`` that reconciles its optimistic bubble.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from typing import Callable

from loguru import logger as _loguru_logger

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.opencode.db_reader import is_message_settled
from imbue.system_interface.harnesses.opencode.db_reader import opencode_db_path
from imbue.system_interface.harnesses.opencode.db_reader import read_changed_messages
from imbue.system_interface.harnesses.opencode.db_reader import read_root_session_id
from imbue.system_interface.harnesses.opencode.session_parser import build_message_events
from imbue.system_interface.harnesses.path_watch import PathWatcher
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher
from imbue.system_interface.harnesses.session_watcher import OnEventsCallback

logger = _loguru_logger


class OpenCodeDbSessionWatcher(AgentSessionWatcher):
    """Watches an opencode agent's ``opencode.db`` and emits parsed UI events."""

    # Declared at class level so a ``build()`` classmethod (no __init__) can assign them while
    # the type checker still resolves every access.
    _agent_id: str
    _state_dir: Path
    _db_path: Path
    _on_events: Callable[[str, list[dict[str, Any]]], None]
    _lock: threading.Lock
    _events: list[dict[str, Any]]
    _event_index: dict[str, int]
    # Already-emitted events a later read superseded (same id, new content), awaiting
    # re-broadcast so the client upgrades its held copy in place (the codex mechanism -- a
    # streaming text/tool-status update IS a supersession of an already-shown event).
    _superseded_pending: dict[str, dict[str, Any]]
    _emitted_count: int
    # The ``time_updated`` watermark: the lowest row-update time not yet known-settled. Rows at
    # or after it are re-scanned every poll; content-supersession dedup makes a re-read of an
    # unchanged row a no-op.
    _updated_cursor: int
    _root_session_id: str | None
    _path_watcher: PathWatcher | None

    @classmethod
    def build(cls, agent_info: AgentInfo, on_events: OnEventsCallback) -> "OpenCodeDbSessionWatcher":
        """Build from the agent record. opencode needs only the state dir: its db and root-session
        marker both live under it."""
        agent_state_dir = agent_info.agent_state_dir
        self = cls.__new__(cls)
        self._agent_id = agent_info.id
        self._state_dir = agent_state_dir
        self._db_path = opencode_db_path(agent_state_dir)
        self._on_events = on_events

        self._lock = threading.Lock()
        self._events = []
        self._event_index = {}
        self._superseded_pending = {}
        self._emitted_count = 0
        self._updated_cursor = 0
        self._root_session_id = None
        self._path_watcher = None
        return self

    def start(self) -> None:
        """Start tailing in a background thread.

        Prime the backlog WITHOUT broadcasting (populate ``_events`` so the REST tail path can
        serve existing history -- the prime-vs-poll split codex/pi use), then watch the db's
        DIRECTORY (recursively): opencode's WAL-mode writes land in ``opencode.db-wal``, so the
        main file's mtime may not move until checkpoint -- watching the dir catches the ``-wal``
        appends. The 1s poll (built into PathWatcher) is the safety net. Idempotent.
        """
        with self._lock:
            self._consume_changes()
        self._path_watcher = PathWatcher.build((self._db_path.parent,), self._emit_unsent)
        self._path_watcher.start()

    def stop(self) -> None:
        """Stop tailing. Idempotent."""
        if self._path_watcher is not None:
            self._path_watcher.stop()

    def _refresh(self) -> None:
        """Bring the in-memory transcript up to date with the db. Incremental (a caught-up
        refresh reads the hot tail only). Called by the loop AND every read method, mirroring
        codex/pi, so a read never depends on the loop having run."""
        with self._lock:
            self._consume_changes()

    def _emit_unsent(self) -> None:
        """Refresh, then broadcast every event not yet sent plus any superseded-in-place events."""
        self._refresh()
        with self._lock:
            pending = self._superseded_pending
            self._superseded_pending = {}
            to_send = list(pending.values()) + self._events[self._emitted_count :]
            self._emitted_count = len(self._events)
        if to_send:
            self._on_events(self._agent_id, to_send)

    def _consume_changes(self) -> None:
        """Read the messages/parts touched since the watermark and ingest their events. Must
        hold ``_lock``.

        Resolves the root session id (the transcript is that single session). Advances the
        watermark to the min update-time of the still-unsettled (streaming) messages -- or, when
        every returned message is settled, to the newest update-time seen -- so the hot tail is
        re-scanned each poll while settled history is not. The watermark is never advanced PAST
        the boundary (no ``+1``), so a same-millisecond new row is never skipped; dedup drops the
        harmless re-read of a settled row.
        """
        root_session_id = read_root_session_id(self._state_dir)
        if root_session_id is None:
            return
        if root_session_id != self._root_session_id:
            # First resolution or a switched root (e.g. an adopt). Re-read from the start; the
            # dedup index makes any re-emit a no-op, and keeping _events is harmless.
            self._root_session_id = root_session_id
            self._updated_cursor = 0

        messages, parts_by_message = read_changed_messages(self._db_path, root_session_id, self._updated_cursor)
        if not messages:
            return

        unsettled_effective: list[int] = []
        all_effective: list[int] = []
        for message in messages:
            parts = parts_by_message.get(message.id, [])
            effective = message.time_updated
            for part in parts:
                effective = max(effective, part.time_updated)
            all_effective.append(effective)
            if not is_message_settled(message, parts):
                unsettled_effective.append(effective)
            for event in build_message_events(message, parts):
                self._ingest_event(event)

        # Low-water mark: re-scan from the oldest still-streaming message next poll; if none is
        # streaming, hold at the newest update seen (so a new row at the same or later time is
        # still caught, and the settled boundary re-reads harmlessly).
        self._updated_cursor = min(unsettled_effective) if unsettled_effective else max(all_effective)

    def _ingest_event(self, event: dict[str, Any]) -> None:
        """Add one parsed event: append a new id, supersede a changed one in place
        (re-broadcasting an already-emitted change), drop an identical duplicate."""
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

    # --- read API (mirrors AgentSessionWatcher) ----------------------------
    # ``session_id`` is accepted for interface parity but inert: opencode is one logical session
    # to the UI (subagents disabled), with no subagent sessions to filter.

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
        """opencode runs without subagents -- always None."""
        return None

    def is_main_session_event(self, event: dict[str, Any]) -> bool:
        """Every opencode event belongs to the single (root) session."""
        return True

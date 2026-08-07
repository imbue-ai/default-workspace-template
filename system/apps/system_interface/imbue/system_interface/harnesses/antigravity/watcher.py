"""Tail an antigravity (agy) agent's own conversation store and emit UI events.

Like the claude/codex watchers, this reads agy's OWN transcript -- never mngr's mirror.
agy stores each conversation as a protobuf SQLite ``.db`` (``steps`` table), so tailing is
SQLite row-offset polling rather than byte-offset file reading: each poll queries rows past
a per-conversation cursor, decodes them (:mod:`agy_transcript`), and maps them to events
(:mod:`session_parser`).

Two-phase emission gives a live activity caption: a tool step's ``tool_call`` is emitted as
soon as its row appears (even ``RUNNING``), its ``tool_result`` only once the row settles.
The cursor advances only through the leading run of terminal rows, so a row seen while
running is re-read (and its result added) once it settles; dedup by ``event_id`` keeps the
already-emitted call from repeating. A row still mid-write decodes to ``TruncatedError`` and
stops the scan for that conversation until the next pass.

No subagents in this cut: agy's ``invoke_subagent`` opens a separate conversation, which we
do not follow yet (``get_subagent_metadata`` -> None, ``is_main_session_event`` -> True).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any
from typing import Callable

from watchdog.observers import Observer

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.antigravity.agy_transcript import TruncatedError
from imbue.system_interface.harnesses.antigravity.agy_transcript import decode_step
from imbue.system_interface.harnesses.antigravity.session_parser import parse_step
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher
from imbue.system_interface.harnesses.session_watcher import OnEventsCallback
from imbue.system_interface.watcher_common import POLL_INTERVAL_SECONDS
from imbue.system_interface.watcher_common import WakeOnChangeHandler

# agy's per-agent conversation store + the capture-hook file listing this agent's
# conversation ids, both relative to the mngr agent state dir.
_CONVERSATIONS_RELATIVE = Path("plugin") / "antigravity" / "home" / ".gemini" / "antigravity-cli" / "conversations"
_CONVERSATION_IDS_RELATIVE = Path("antigravity_conversation_ids")

_STEPS_QUERY = "SELECT idx, step_type, status, step_payload FROM steps WHERE idx >= ? ORDER BY idx"


class AntigravitySessionWatcher(AgentSessionWatcher):
    """Watches an agy agent's conversation ``.db``(s) and emits parsed UI events."""

    _agent_id: str
    _state_dir: Path
    _on_events: Callable[[str, list[dict[str, Any]]], None]
    _lock: threading.Lock
    _events: list[dict[str, Any]]
    _index_by_id: dict[str, int]
    _emitted_ids: set[str]
    _scan_from: dict[str, int]
    _wake: threading.Event
    _stopping: threading.Event
    _thread: threading.Thread | None
    _observer: Any

    @classmethod
    def build(cls, agent_info: AgentInfo, on_events: OnEventsCallback) -> "AntigravitySessionWatcher":
        self = cls.__new__(cls)
        self._agent_id = agent_info.id
        self._state_dir = agent_info.agent_state_dir
        self._on_events = on_events
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._index_by_id: dict[str, int] = {}
        self._emitted_ids: set[str] = set()
        # Per-conversation cursor: the lowest idx not yet known-terminal (re-read until it is).
        self._scan_from: dict[str, int] = {}
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._observer: Any = None
        return self

    # --- paths ---------------------------------------------------------------------------

    def _conversations_dir(self) -> Path:
        return self._state_dir / _CONVERSATIONS_RELATIVE

    def _conversation_ids_file(self) -> Path:
        return self._state_dir / _CONVERSATION_IDS_RELATIVE

    def _conversation_ids(self) -> list[str]:
        """This agent's conversation ids in order, from the capture-hook file (deduped)."""
        path = self._conversation_ids_file()
        if not path.is_file():
            return []
        seen: list[str] = []
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return []
        for line in lines:
            candidate = line.strip()
            if candidate and candidate not in seen:
                seen.append(candidate)
        return seen

    # --- lifecycle -----------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        # Prime the backlog WITHOUT broadcasting it -- it is delivered via the REST tail path,
        # not the live stream (mirrors claude's prime-vs-poll split).
        with self._lock:
            self._collect_new_events()
        self._observer = Observer()
        handler = WakeOnChangeHandler(self._wake)
        # Watch whatever exists now; the 1s poll safety net covers a dir/file created later.
        for directory in (self._state_dir, self._conversations_dir()):
            if directory.is_dir():
                self._observer.schedule(handler, str(directory), recursive=False)
        self._observer.start()
        self._thread = threading.Thread(target=self._run, name=f"agy-watcher-{self._agent_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2.0)
            self._observer = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stopping.is_set():
            self._wake.wait(timeout=POLL_INTERVAL_SECONDS)
            self._wake.clear()
            if self._stopping.is_set():
                return
            with self._lock:
                pending = self._collect_new_events()
            if pending:
                self._on_events(self._agent_id, pending)

    # --- scanning ------------------------------------------------------------------------

    def _collect_new_events(self) -> list[dict[str, Any]]:
        """Scan every conversation for new/settled rows; append + return newly-emitted events.
        Caller holds ``self._lock``."""
        pending: list[dict[str, Any]] = []
        for conv_id in self._conversation_ids():
            db_path = self._conversations_dir() / f"{conv_id}.db"
            if db_path.is_file():
                self._scan_conversation(conv_id, db_path, pending)
        return pending

    def _scan_conversation(self, conv_id: str, db_path: Path, pending: list[dict[str, Any]]) -> None:
        scan_from = self._scan_from.get(conv_id, 0)
        rows = self._read_rows(db_path, scan_from)
        terminal_prefix_end = scan_from - 1
        for idx, step_type, status, payload in rows:
            try:
                decoded = decode_step(conv_id, idx, step_type, status, bytes(payload))
            except TruncatedError:
                # This row is mid-write; stop here so it is re-read whole next pass.
                break
            for event in parse_step(decoded):
                event_id = event["event_id"]
                if event_id in self._emitted_ids:
                    continue
                self._emitted_ids.add(event_id)
                self._index_by_id[event_id] = len(self._events)
                self._events.append(event)
                pending.append(event)
            # Advance the cursor only through the unbroken leading run of terminal rows, so a
            # still-running row (and anything after it) is re-scanned until it settles.
            if decoded.is_terminal and idx == terminal_prefix_end + 1:
                terminal_prefix_end = idx
        self._scan_from[conv_id] = terminal_prefix_end + 1

    def _read_rows(self, db_path: Path, scan_from: int) -> list[tuple[int, int, int, bytes]]:
        # Read-only + WAL-aware; agy is concurrently writing. A transient lock/checkpoint
        # surfaces as sqlite3.Error -> skip this conversation this pass, retry next.
        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return []
        try:
            return connection.execute(_STEPS_QUERY, (scan_from,)).fetchall()
        except sqlite3.Error:
            return []
        finally:
            connection.close()

    # --- read interface (single flat session; subagents not surfaced) --------------------

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)

    def get_tail_events(self, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events[-limit:]) if limit > 0 else []

    def get_backfill_events(
        self, before_event_id: str, limit: int, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            index = self._index_by_id.get(before_event_id)
            if index is None:
                return []
            return list(self._events[max(0, index - limit) : index])

    def get_forward_events(
        self, after_event_id: str, limit: int, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            index = self._index_by_id.get(after_event_id)
            if index is None:
                return []
            return list(self._events[index + 1 : index + 1 + limit])

    def get_events_at_offset(self, offset: int, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if offset < 0 or limit <= 0:
                return []
            return list(self._events[offset : offset + limit])

    def get_event_offset(self, event_id: str, session_id: str | None = None) -> int:
        with self._lock:
            return self._index_by_id.get(event_id, -1)

    def get_total_event_count(self, session_id: str | None = None) -> int:
        with self._lock:
            return len(self._events)

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        return None

    def is_main_session_event(self, event: dict[str, Any]) -> bool:
        return True

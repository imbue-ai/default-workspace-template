"""Read-only transcript watcher for non-Claude harnesses.

``AgentSessionWatcher`` parses Claude's raw session JSONL directly -- rich
(subagent linkage, byte-offset paging for an unbounded transcript) but
Claude-specific. The other three harnesses (codex, antigravity, opencode)
have no raw format worth a bespoke parser here: each already writes the
same harness-agnostic transcript mngr's own per-harness converters produce
(see ``imbue.mngr.agents.common_transcript_records``), and ``mngr
transcript`` already reads it. This watcher reuses that mechanism directly
(``discover_event_sources`` + ``read_all_historical_events``) instead of
re-deriving a parser: a poll loop re-reads the whole common-transcript file
-- small for these harnesses, with no subagents and no long transcript
history yet, so a byte-offset index would be premature -- and diffs against
previously seen event ids.

Each record is adapted into the same event-dict shape ``session_parser.py``
produces for Claude (see ``frontend/src/models/Response.ts``), so the chat
view renders identically regardless of which harness produced the event.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from collections.abc import Mapping
from typing import Any

from loguru import logger as _loguru_logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.events import EventSourceInfo
from imbue.mngr.api.events import EventsTarget
from imbue.mngr.api.events import discover_event_sources
from imbue.mngr.api.events import read_all_historical_events
from imbue.mngr.api.events import refresh_events_target
from imbue.mngr.api.events import resolve_events_target
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.system_interface.agent_discovery import get_mngr_context
from imbue.system_interface.common_transcript_auth_patterns import is_auth_error_text
from imbue.system_interface.watcher_common import POLL_INTERVAL_SECONDS

logger = _loguru_logger

_COMMON_TRANSCRIPT_SUFFIX = "common_transcript"


def _find_common_transcript_source(sources: list[EventSourceInfo]) -> EventSourceInfo | None:
    """Return the source whose path is (or ends with) 'common_transcript'.

    Excludes the converter's own `logs/common_transcript` log source. Mirrors
    ``imbue.mngr.cli.transcript._find_common_transcript_source``.
    """
    for source in sources:
        path = source.source_path
        if path.startswith("logs/"):
            continue
        if path == _COMMON_TRANSCRIPT_SUFFIX or path.endswith(f"/{_COMMON_TRANSCRIPT_SUFFIX}"):
            return source
    return None


def _map_tool_call(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "tool_call_id": raw.get("tool_call_id", ""),
        "tool_name": raw.get("tool_name", ""),
        "input_preview": raw.get("input_preview", ""),
    }


def _map_usage(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Normalize the schema's free-form usage mapping to the frontend's fixed shape.

    None passes through unchanged -- codex, antigravity, and opencode's converters
    all emit `usage: null` today (none report token counts yet).
    """
    if raw is None:
        return None
    return {
        "input_tokens": raw.get("input_tokens", 0),
        "output_tokens": raw.get("output_tokens", 0),
        "cache_read_tokens": raw.get("cache_read_tokens"),
        "cache_write_tokens": raw.get("cache_write_tokens"),
    }


def _to_frontend_event(data: Mapping[str, Any]) -> dict[str, Any] | None:
    """Adapt one common-transcript record into the frontend's TranscriptEvent shape.

    Returns None for a record type the frontend does not render (there are
    none today -- the schema is closed to exactly these three -- but a future
    record type should degrade to "skip", not crash the poll loop).
    """
    event_type = data.get("type")
    base = {
        "timestamp": data.get("timestamp", ""),
        "event_id": data.get("event_id", ""),
        "source": data.get("source", ""),
    }
    if event_type == "user_message":
        return {**base, "type": "user_message", "role": data.get("role", "user"), "content": data.get("content", "")}
    if event_type == "assistant_message":
        text = data.get("text", "")
        # `source` is "<harness>/common_transcript" (see
        # common_transcript_records.py's EventRecord.source convention) --
        # the harness prefix identifies which pattern set to check.
        harness_prefix = str(data.get("source", "")).split("/", 1)[0]
        return {
            **base,
            "type": "assistant_message",
            "model": data.get("model") or "unknown",
            "text": text,
            "tool_calls": [_map_tool_call(tc) for tc in data.get("tool_calls", [])],
            "stop_reason": data.get("finish_reason"),
            "usage": _map_usage(data.get("usage")),
            "is_auth_error": is_auth_error_text(harness_prefix, text),
        }
    if event_type == "tool_result":
        return {
            **base,
            "type": "tool_result",
            "tool_call_id": data.get("tool_call_id", ""),
            "tool_name": data.get("tool_name", "unknown"),
            "output": data.get("output", ""),
            "is_error": bool(data.get("is_error", False)),
        }
    return None


class CommonTranscriptWatcher:
    """Watches one non-Claude agent's mngr common-transcript stream.

    All access to ``_events``/``_index_by_id``/``_seen_ids`` is guarded by
    ``_lock``, since the poll thread and Flask request threads touch them
    concurrently -- mirroring ``AgentSessionWatcher``'s locking discipline.
    ``_target`` is touched only by the poll thread, so it needs no lock.
    """

    def __init__(self, agent_id: str, on_events: Callable[[str, list[dict[str, Any]]], None]) -> None:
        self._agent_id = agent_id
        self._on_events = on_events

        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._index_by_id: dict[str, int] = {}
        self._seen_ids: set[str] = set()

        self._target: EventsTarget | None = None
        self._mngr_ctx: MngrContext | None = None
        self._cg: ConcurrencyGroup | None = None
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start polling in a background thread."""
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"common-transcript-watcher-{self._agent_id}"
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop polling and release the mngr context, if one was acquired."""
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        if self._cg is not None:
            self._cg.__exit__(None, None, None)
            self._cg = None

    def _run(self) -> None:
        # Prime the backlog without broadcasting it: like AgentSessionWatcher, the
        # initial transcript reaches clients via the REST tail/backfill path, so
        # broadcasting it here too would double-deliver it over SSE.
        self._poll_once(should_broadcast=False)
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=POLL_INTERVAL_SECONDS)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            self._poll_once(should_broadcast=True)

    def _ensure_mngr_context(self) -> MngrContext | None:
        """Acquire the mngr context once and hold it for the watcher's lifetime.

        A full acquisition does real disk I/O (config load + plugin discovery),
        so this is paid once per watcher rather than once per poll -- unlike a
        one-shot caller (e.g. agent_discovery.py's other uses), this watcher polls
        every ``POLL_INTERVAL_SECONDS`` for as long as the agent's chat panel is
        open. Retries on a transient acquisition failure rather than giving up
        permanently after one bad poll (mirrors the retry-next-poll handling
        already used for a missing common-transcript source below).
        """
        if self._mngr_ctx is not None:
            return self._mngr_ctx
        try:
            self._mngr_ctx, self._cg = get_mngr_context()
        except (MngrError, OSError) as e:
            logger.debug("Failed to acquire mngr context for agent {}: {}", self._agent_id, e)
            return None
        return self._mngr_ctx

    def _poll_once(self, should_broadcast: bool) -> None:
        mngr_ctx = self._ensure_mngr_context()
        if mngr_ctx is None:
            return
        try:
            target = self._current_target(mngr_ctx)
            sources = discover_event_sources(target)
            source = _find_common_transcript_source(sources)
            if source is None:
                # The converter has not produced its output file yet (agent just
                # created, or offline before its first event) -- retry next poll.
                return
            records, _ = read_all_historical_events(target, [source], (), ())
        except (MngrError, OSError) as e:
            logger.debug("Common-transcript poll failed for agent {}: {}", self._agent_id, e)
            return

        new_events: list[dict[str, Any]] = []
        with self._lock:
            for record in records:
                if record.event_id in self._seen_ids:
                    continue
                mapped = _to_frontend_event(record.data)
                if mapped is None:
                    continue
                self._seen_ids.add(record.event_id)
                self._index_by_id[mapped["event_id"]] = len(self._events)
                self._events.append(mapped)
                new_events.append(mapped)

        if new_events and should_broadcast:
            self._on_events(self._agent_id, new_events)

    def _current_target(self, mngr_ctx: MngrContext) -> EventsTarget:
        """Resolve the events target once, then cheaply refresh it on later polls.

        A full resolve does agent discovery; ``refresh_events_target`` only
        re-checks online status against the already-resolved provider/host_id, the
        same cheap-refresh split ``stream_all_events`` uses for its online/offline
        transitions.
        """
        if self._target is None:
            self._target = resolve_events_target(AgentAddress(agent=AgentId(self._agent_id)), mngr_ctx)
        else:
            self._target = refresh_events_target(self._target)
        return self._target

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Return every parsed event. ``session_id`` is accepted for interface
        parity with ``AgentSessionWatcher`` but unused -- there is exactly one
        stream per agent here, no per-session/subagent selection."""
        with self._lock:
            return list(self._events)

    def get_tail_events(self, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._lock:
            return list(self._events[-limit:])

    def get_backfill_events(
        self, before_event_id: str, limit: int = 50, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._lock:
            idx = self._index_by_id.get(before_event_id)
            if idx is None:
                return []
            start = max(0, idx - limit)
            return list(self._events[start:idx])

    def get_forward_events(
        self, after_event_id: str, limit: int = 50, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._lock:
            idx = self._index_by_id.get(after_event_id)
            if idx is None:
                return []
            start = idx + 1
            return list(self._events[start : start + limit])

    def get_events_at_offset(self, offset: int, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._lock:
            start = max(0, offset)
            return list(self._events[start : start + limit])

    def get_event_offset(self, event_id: str, session_id: str | None = None) -> int:
        with self._lock:
            idx = self._index_by_id.get(event_id)
            return idx if idx is not None else -1

    def get_total_event_count(self, session_id: str | None = None) -> int:
        with self._lock:
            return len(self._events)

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        """No subagent concept in the shared common-transcript schema."""
        return None

    def is_main_session_event(self, event: dict[str, Any]) -> bool:
        """Every event belongs to the one shared stream -- nothing to filter out."""
        return True

"""The shared full-residency transcript store and the watcher base built on it.

Every harness watcher keeps ONE agent's parsed transcript fully resident: an ordered set
of *lanes* (one per session file for claude; a single merged timeline for codex/pi), each
an append-ordered list of parsed event dicts, plus one agent-wide ``event_id`` index.
Full residency is affordable because the parsers emit payload-free events -- tool inputs,
tool outputs, and thinking stay on disk and are re-read on demand through the per-event
source byte ranges this store tracks (see :meth:`TranscriptStore.source_of`). The wire
therefore carries identity, prose, labels, and small derived stamps -- never raw payloads
-- and the resident cost per event is correspondingly small.

This replaces the claude watcher's two-tier locator/body-LRU cache: at real transcript
sizes the bounded design saved little memory while costing a re-parse-from-disk path and
its correctness invariants, and the O(N)-per-event ``get_all_events`` rematerialisation it
forced was itself the dominant memory/CPU pathology. Residency is bounded instead by the
payload-free event shape, and lifetime by watcher eviction on agent stop/destroy
(``SystemInterfaceState.stop_and_remove_watcher``).

Locking: the store takes no lock of its own. The owning watcher guards every call with
its single lock (held across file reads and parsing -- cheap and incremental -- but never
across the ``on_events`` fan-out), exactly the discipline the per-harness watchers already
follow.

Emission bookkeeping lives here too: each lane carries an ``emitted_count`` high-water
mark (decoupled from parsing, so a concurrent HTTP read that advanced a byte cursor never
robs the poll loop of events to emit), and an in-place supersession of an already-emitted
event -- codex re-serialising history, claude's subagent enrichment landing late -- is
queued for re-broadcast so connected clients upgrade their held copy.
"""

from __future__ import annotations

import json
import threading
from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import Any
from typing import Callable

from loguru import logger as _loguru_logger

from imbue.system_interface.harnesses.path_watch import PathWatcher
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher

logger = _loguru_logger

# Where one event's payloads live on disk: (file path, byte offset, byte length) of the
# source line. Re-reading exactly that span and re-parsing reconstructs the payloads.
EventSource = tuple[Path, int, int]


def is_complete_json_object(fragment: bytes) -> bool:
    """Whether ``fragment`` parses as a complete JSON value on its own.

    Used to decide whether a trailing line without a newline terminator is a finished
    record written without a trailing ``\\n`` (parses -> complete) or an in-progress write
    that must be retained for the next read (does not parse).
    """
    stripped = fragment.strip()
    if not stripped:
        return False
    try:
        json.loads(stripped)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return True


def split_at_last_complete_line(data: bytes) -> tuple[bytes, bytes]:
    """Split raw appended bytes into ``(complete_lines, trailing_fragment)``.

    Only complete lines are safe to parse and consume: advancing a byte cursor past an
    incomplete trailing line would lose that record permanently once it is finished, and
    decoding a boundary that splits a multi-byte UTF-8 sequence would corrupt it. A
    trailing fragment with no newline is folded into the complete half only when it parses
    as JSON on its own (a final record written without a trailing newline).
    """
    if data.endswith(b"\n"):
        return data, b""
    newline_index = data.rfind(b"\n")
    fragment = data if newline_index == -1 else data[newline_index + 1 :]
    if is_complete_json_object(fragment):
        return data, b""
    if newline_index == -1:
        return b"", data
    return data[: newline_index + 1], fragment


def iter_line_spans(data: bytes, base_offset: int) -> list[tuple[int, int, bytes]]:
    """Split ``data`` into ``(byte_offset, byte_len, line_bytes)`` per line.

    ``byte_offset`` is the absolute file offset of the line; ``byte_len`` includes the
    trailing newline (the final line may lack one), so re-reading exactly ``byte_len``
    bytes at ``byte_offset`` reproduces the line.
    """
    spans: list[tuple[int, int, bytes]] = []
    pos = 0
    length = len(data)
    while pos < length:
        newline_index = data.find(b"\n", pos)
        if newline_index == -1:
            line = data[pos:]
            next_pos = length
        else:
            line = data[pos : newline_index + 1]
            next_pos = newline_index + 1
        spans.append((base_offset + pos, len(line), line))
        pos = next_pos
    return spans


def read_source_line(source: EventSource) -> str | None:
    """Re-read the single source line ``source`` addresses, or None when unreadable."""
    path, byte_offset, byte_len = source
    try:
        with open(path, "rb") as f:
            f.seek(byte_offset)
            raw = f.read(byte_len)
    except OSError as e:
        logger.debug("Failed to re-read source line {}@{}: {}", path, byte_offset, e)
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        logger.debug("UTF-8 decode error re-reading {}@{}: {}", path, byte_offset, e)
        return None


class _Lane:
    """One ordered slice of an agent's timeline: a session file's events for claude, the
    single merged timeline for codex/pi."""

    __slots__ = ("lane_id", "events", "emitted_count")

    def __init__(self, lane_id: str) -> None:
        self.lane_id = lane_id
        self.events: list[dict[str, Any]] = []
        self.emitted_count = 0


class TranscriptStore:
    """One agent's fully-resident parsed transcript: ordered lanes + one id index."""

    def __init__(self) -> None:
        self._lane_by_id: dict[str, _Lane] = {}
        self._lane_order: list[str] = []
        # event_id -> (lane_id, index within lane). The single dedup/pagination index.
        self._ref_by_event_id: dict[str, tuple[str, int]] = {}
        # Already-emitted events superseded or enriched since the last emit, keyed by id so
        # repeated changes collapse to one re-broadcast of the (mutated-in-place) dict.
        self._rebroadcast_pending: dict[str, dict[str, Any]] = {}
        # Payload byte ranges, kept beside (not on) the events so nothing needs stripping
        # before events reach the wire. A supersession updates the range to the newest copy.
        self._source_by_event_id: dict[str, EventSource] = {}
        # A second range for harnesses whose readable thinking lives on a different source
        # line than the event itself (codex reasoning items).
        self._thinking_source_by_event_id: dict[str, EventSource] = {}

    # -- lanes ----------------------------------------------------------------------------

    def ensure_lane(self, lane_id: str, insert_index: int | None = None) -> None:
        """Register ``lane_id`` if new, at ``insert_index`` in the lane order (default: end)."""
        if lane_id in self._lane_by_id:
            return
        self._lane_by_id[lane_id] = _Lane(lane_id)
        if insert_index is None:
            self._lane_order.append(lane_id)
        else:
            self._lane_order.insert(insert_index, lane_id)

    def has_lane(self, lane_id: str) -> bool:
        return lane_id in self._lane_by_id

    def lane_ids(self) -> list[str]:
        return list(self._lane_order)

    def reset_lane(self, lane_id: str) -> None:
        """Drop a lane's events (a truncated/rewritten source file is re-read from scratch)."""
        lane = self._lane_by_id.get(lane_id)
        if lane is None:
            return
        for event in lane.events:
            event_id = event["event_id"]
            self._ref_by_event_id.pop(event_id, None)
            self._rebroadcast_pending.pop(event_id, None)
            self._source_by_event_id.pop(event_id, None)
            self._thinking_source_by_event_id.pop(event_id, None)
        lane.events = []
        lane.emitted_count = 0

    # -- writes ---------------------------------------------------------------------------

    def ingest(self, lane_id: str, event: dict[str, Any], source: EventSource | None = None) -> bool:
        """Add one parsed event: append a new id, supersede a changed one in place, refresh
        the source range of an identical re-serialisation. Returns True iff newly appended.

        A supersession of an already-emitted event is queued for re-broadcast so the client
        upgrades its held copy in place; a not-yet-emitted one needs no entry (the tail
        broadcast already carries the latest content).
        """
        event_id = event["event_id"]
        ref = self._ref_by_event_id.get(event_id)
        if ref is not None:
            ref_lane_id, index = ref
            lane = self._lane_by_id[ref_lane_id]
            if source is not None:
                self._source_by_event_id[event_id] = source
            if lane.events[index] != event:
                lane.events[index] = event
                if index < lane.emitted_count:
                    self._rebroadcast_pending[event_id] = event
            return False
        self.ensure_lane(lane_id)
        lane = self._lane_by_id[lane_id]
        self._ref_by_event_id[event_id] = (lane_id, len(lane.events))
        lane.events.append(event)
        if source is not None:
            self._source_by_event_id[event_id] = source
        return True

    def register_rebroadcast(self, event_id: str) -> None:
        """Queue an already-emitted event (mutated in place, e.g. enriched) for re-broadcast."""
        ref = self._ref_by_event_id.get(event_id)
        if ref is None:
            return
        lane_id, index = ref
        lane = self._lane_by_id[lane_id]
        if index < lane.emitted_count:
            self._rebroadcast_pending[event_id] = lane.events[index]

    def take_unemitted(self) -> list[dict[str, Any]]:
        """Drain the re-broadcast queue plus every lane's not-yet-emitted tail, advancing
        the high-water marks. Re-broadcasts come first; order within is irrelevant (the
        client keys upgrades on ``event_id``, not position)."""
        to_send = list(self._rebroadcast_pending.values())
        self._rebroadcast_pending = {}
        for lane_id in self._lane_order:
            lane = self._lane_by_id[lane_id]
            to_send.extend(lane.events[lane.emitted_count :])
            lane.emitted_count = len(lane.events)
        return to_send

    def mark_all_emitted(self) -> None:
        """Mark the whole current backlog as already delivered (the priming path: the
        initial transcript reaches clients via the REST tail, never the live stream)."""
        self._rebroadcast_pending = {}
        for lane in self._lane_by_id.values():
            lane.emitted_count = len(lane.events)

    # -- payload sources ------------------------------------------------------------------

    def source_of(self, event_id: str) -> EventSource | None:
        return self._source_by_event_id.get(event_id)

    def thinking_source_of(self, event_id: str) -> EventSource | None:
        return self._thinking_source_by_event_id.get(event_id)

    def set_thinking_source(self, event_id: str, source: EventSource) -> None:
        self._thinking_source_by_event_id[event_id] = source

    # -- reads ----------------------------------------------------------------------------

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        ref = self._ref_by_event_id.get(event_id)
        if ref is None:
            return None
        lane_id, index = ref
        return self._lane_by_id[lane_id].events[index]

    def _selected(self, lane_ids: list[str]) -> list[_Lane]:
        return [self._lane_by_id[lane_id] for lane_id in lane_ids if lane_id in self._lane_by_id]

    def all_events(self, lane_ids: list[str]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for lane in self._selected(lane_ids):
            events.extend(lane.events)
        return events

    def total(self, lane_ids: list[str]) -> int:
        return sum(len(lane.events) for lane in self._selected(lane_ids))

    def tail(self, lane_ids: list[str], limit: int) -> list[dict[str, Any]]:
        """The last ``limit`` events across the selection -- O(limit + lane count)."""
        if limit <= 0:
            return []
        lanes = self._selected(lane_ids)
        collected: list[dict[str, Any]] = []
        needed = limit
        pos = len(lanes) - 1
        while needed > 0 and pos >= 0:
            events = lanes[pos].events
            start = max(0, len(events) - needed)
            collected = events[start:] + collected
            needed -= len(events) - start
            pos -= 1
        return collected

    def _locate(self, lanes: list[_Lane], event_id: str) -> tuple[int, int] | None:
        """``(lane position within selection, index within lane)`` of ``event_id``, or None."""
        ref = self._ref_by_event_id.get(event_id)
        if ref is None:
            return None
        ref_lane_id, index = ref
        for lane_pos, lane in enumerate(lanes):
            if lane.lane_id == ref_lane_id:
                return lane_pos, index
        return None

    def before(self, lane_ids: list[str], before_event_id: str, limit: int) -> list[dict[str, Any]]:
        """Up to ``limit`` events immediately before ``before_event_id`` -- O(limit + lanes)."""
        if limit <= 0:
            return []
        lanes = self._selected(lane_ids)
        located = self._locate(lanes, before_event_id)
        if located is None:
            return []
        lane_pos, index = located
        collected: list[dict[str, Any]] = []
        needed = limit
        pos = lane_pos
        while needed > 0 and pos >= 0:
            events = lanes[pos].events
            end = index if pos == lane_pos else len(events)
            start = max(0, end - needed)
            collected = events[start:end] + collected
            needed -= end - start
            pos -= 1
        return collected

    def after(self, lane_ids: list[str], after_event_id: str, limit: int) -> list[dict[str, Any]]:
        """Up to ``limit`` events immediately after ``after_event_id`` -- O(limit + lanes)."""
        if limit <= 0:
            return []
        lanes = self._selected(lane_ids)
        located = self._locate(lanes, after_event_id)
        if located is None:
            return []
        lane_pos, index = located
        collected: list[dict[str, Any]] = []
        needed = limit
        pos = lane_pos
        while needed > 0 and pos < len(lanes):
            events = lanes[pos].events
            start = index + 1 if pos == lane_pos else 0
            end = min(len(events), start + needed)
            collected.extend(events[start:end])
            needed -= end - start
            pos += 1
        return collected

    def at_offset(self, lane_ids: list[str], offset: int, limit: int) -> list[dict[str, Any]]:
        """``limit`` events starting at global index ``offset`` across the selection."""
        if limit <= 0:
            return []
        collected: list[dict[str, Any]] = []
        skip = max(0, offset)
        needed = limit
        for lane in self._selected(lane_ids):
            count = len(lane.events)
            if skip >= count:
                skip -= count
                continue
            start = skip
            skip = 0
            end = min(count, start + needed)
            collected.extend(lane.events[start:end])
            needed -= end - start
            if needed <= 0:
                break
        return collected

    def offset_of(self, lane_ids: list[str], event_id: str) -> int:
        """Global index of ``event_id`` within the selection, or -1."""
        lanes = self._selected(lane_ids)
        located = self._locate(lanes, event_id)
        if located is None:
            return -1
        lane_pos, index = located
        return sum(len(lanes[pos].events) for pos in range(lane_pos)) + index


class StoreBackedWatcher(AgentSessionWatcher, ABC):
    """Shared watcher scaffolding over a :class:`TranscriptStore`.

    Owns the single lock, the store, and the :class:`PathWatcher` watch loop; subclasses
    supply discovery + incremental consumption (:meth:`_refresh_locked`), the paths to
    watch, the lane selection for a ``session_id``, and payload re-parsing for the detail
    endpoint. ``start`` primes the backlog without broadcasting it (the initial transcript
    is served over REST; flooding the bounded SSE queues with history would evict clients).
    """

    _agent_id: str
    _on_events: Callable[[str, list[dict[str, Any]]], None]
    _lock: threading.Lock
    _store: TranscriptStore
    _path_watcher: PathWatcher | None

    def _init_store_watcher(self, agent_id: str, on_events: Callable[[str, list[dict[str, Any]]], None]) -> None:
        self._agent_id = agent_id
        self._on_events = on_events
        self._lock = threading.Lock()
        self._store = TranscriptStore()
        self._path_watcher = None

    # -- per-harness hooks ----------------------------------------------------------------

    @abstractmethod
    def _watch_paths(self) -> tuple[Path, ...]:
        """The paths whose changes should wake the emit cycle."""

    @abstractmethod
    def _refresh_locked(self) -> None:
        """Bring the store up to date with disk (discovery + incremental reads). Lock held."""

    def _selected_lane_ids_locked(self, session_id: str | None) -> list[str]:
        """The ordered lanes a read for ``session_id`` covers. Default: every lane."""
        return self._store.lane_ids()

    def _filter_broadcast(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop events the live stream must not carry (codex's ledger-owned user turns)."""
        return events

    def _before_broadcast(self) -> None:
        """Push side-channel state that must precede the event broadcast (queue snapshots:
        the A3b depart-before-arrive ordering). Called outside the lock. No-op default."""

    def _parse_detail(
        self, event: dict[str, Any], source_line: str | None, thinking_line: str | None
    ) -> dict[str, Any] | None:
        """Reconstruct ``event``'s payloads from its source line(s), or None when the line
        no longer matches the event. Default: no payloads (harness without deferral)."""
        return None

    def _find_detail_source_fallback(self, event: dict[str, Any]) -> str | None:
        """Scan for the event's source line when the recorded byte range went stale (the
        file was rewritten under us). Default: no fallback."""
        return None

    # -- lifecycle ------------------------------------------------------------------------

    def start(self) -> None:
        """Prime the backlog (unemitted -> emitted, no broadcast) and begin watching."""
        if self._path_watcher is not None:
            return
        self._prime()
        self._path_watcher = PathWatcher.build(self._watch_paths(), self._emit_cycle)
        self._path_watcher.start()

    def _prime(self) -> None:
        """Parse the existing backlog and mark it emitted, in one lock hold.

        The initial transcript is delivered to clients via the REST tail/backfill path, so
        it must not also be broadcast through ``on_events`` (that would flood the bounded
        SSE queues for long histories). One lock hold so a concurrent read cannot slip
        events in between the fill and the mark that then never reach SSE clients.
        """
        with self._lock:
            self._refresh_locked()
            self._store.mark_all_emitted()

    def stop(self) -> None:
        if self._path_watcher is not None:
            self._path_watcher.stop()

    def _emit_cycle(self) -> None:
        """Refresh, then deliver every not-yet-emitted event exactly once.

        Emission is driven by the store's high-water marks rather than by what this call
        parsed, so events a concurrent HTTP read pulled in are still delivered. The
        ``_before_broadcast`` hook (queue snapshots) runs before the events go out, and the
        fan-out callback runs outside the lock.
        """
        with self._lock:
            self._refresh_locked()
            to_send = self._filter_broadcast(self._store.take_unemitted())
        self._before_broadcast()
        if to_send:
            self._on_events(self._agent_id, to_send)

    # -- read API -------------------------------------------------------------------------

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_locked()
            return self._store.all_events(self._selected_lane_ids_locked(session_id))

    def get_tail_events(self, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_locked()
            return self._store.tail(self._selected_lane_ids_locked(session_id), limit)

    def get_backfill_events(
        self, before_event_id: str, limit: int = 50, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_locked()
            return self._store.before(self._selected_lane_ids_locked(session_id), before_event_id, limit)

    def get_forward_events(
        self, after_event_id: str, limit: int = 50, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_locked()
            return self._store.after(self._selected_lane_ids_locked(session_id), after_event_id, limit)

    def get_events_at_offset(self, offset: int, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_locked()
            return self._store.at_offset(self._selected_lane_ids_locked(session_id), offset, limit)

    def get_event_offset(self, event_id: str, session_id: str | None = None) -> int:
        with self._lock:
            self._refresh_locked()
            return self._store.offset_of(self._selected_lane_ids_locked(session_id), event_id)

    def get_total_event_count(self, session_id: str | None = None) -> int:
        with self._lock:
            self._refresh_locked()
            return self._store.total(self._selected_lane_ids_locked(session_id))

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        return None

    def is_main_session_event(self, event: dict[str, Any]) -> bool:
        return True

    # -- payload detail -------------------------------------------------------------------

    def get_event_detail(self, event_id: str) -> dict[str, Any] | None:
        """The full deferred payloads for one event, re-read statelessly from disk.

        Nothing read here is cached backend-side -- a detail request is a user click, and
        holding payloads resident is exactly what the payload-free event shape avoids. When
        the recorded byte range no longer parses back to the event (the file was rewritten
        and the store has not caught up), the harness's fallback scan gets one chance
        before the caller reports the payload unavailable.
        """
        with self._lock:
            self._refresh_locked()
            event = self._store.get_event(event_id)
            source = self._store.source_of(event_id)
            thinking_source = self._store.thinking_source_of(event_id)
        if event is None:
            return None
        source_line = read_source_line(source) if source is not None else None
        thinking_line = read_source_line(thinking_source) if thinking_source is not None else None
        detail = self._parse_detail(event, source_line, thinking_line)
        if detail is not None:
            return detail
        fallback_line = self._find_detail_source_fallback(event)
        if fallback_line is None:
            return None
        return self._parse_detail(event, fallback_line, thinking_line)

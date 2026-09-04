"""The frozen transcripts of a chat's retired agents, and the reader that stitches them on.

A chat outlives the agents that back it. When a harness switch retires an agent,
that agent is destroyed and its transcript files go with it -- so before the
destroy the whole parsed transcript is copied here, one file per retired
segment. Reads of the chat then serve the archived segments ahead of the live
agent's, and the user scrolls back through a conversation whose earlier turns
were spoken by a harness that no longer exists.

Two things are captured, not one. Events are payload-free by design (see
``harnesses.events``): tool inputs, outputs, and thinking stay on the agent's
disk and are re-read on demand. That deferral cannot survive the destroy, so
each archived event carries its detail payload INLINE, materialized eagerly
while the agent is still alive. This makes an archived segment strictly
self-contained: nothing it serves depends on a path that still exists.

The archive is append-only and never rewritten. A segment file is written once,
at retirement, and afterwards only read or deleted with the chat.
"""

import json
import threading
from pathlib import Path
from typing import Any

from loguru import logger as _loguru_logger
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.atomic_write import write_json_atomic
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher
from imbue.system_interface.harnesses.session_watcher import FlushSendCallback
from imbue.system_interface.harnesses.session_watcher import IsAliveCallback
from imbue.system_interface.harnesses.session_watcher import OnEventsCallback
from imbue.system_interface.harnesses.session_watcher import QueueSnapshotCallback
from imbue.system_interface.harnesses.transcript_store import StoreBackedWatcher
from imbue.system_interface.models import ChatId

_ARCHIVES_SUBDIR = "chat_archives"

# One archived event: the payload-free event exactly as it went over the wire, plus
# the detail payload that was still fetchable when it was captured (None when the
# harness defers nothing for it, or when the fetch came back empty).
_EVENT_KEY = "event"
_DETAIL_KEY = "detail"


def archives_dir_for_layout_dir(layout_dir: Path) -> Path:
    """Where retired segments' transcripts live for a workspace laid out under ``layout_dir``."""
    return layout_dir / _ARCHIVES_SUBDIR


class TranscriptArchive(MutableModel):
    """Every chat's retired-segment transcripts in one workspace.

    An ``archives_dir`` of None keeps the archive in memory only, mirroring
    ``ChatRegistry(chats_dir=None)``: a dev or test server with no workspace to
    persist into still behaves consistently within its own lifetime.
    """

    model_config = {"extra": "forbid", "frozen": False}

    archives_dir: Path | None
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # Segment payloads for an archive with nowhere to persist, keyed exactly as the
    # files would be named.
    _in_memory: dict[str, list[dict[str, Any]]] = PrivateAttr(default_factory=dict)

    def _segment_key(self, chat_id: ChatId, agent_id: str) -> str:
        return f"{chat_id}.{agent_id}"

    def _segment_path(self, chat_id: ChatId, agent_id: str) -> Path | None:
        if self.archives_dir is None:
            return None
        return self.archives_dir / f"{self._segment_key(chat_id, agent_id)}.jsonl"

    def capture(self, chat_id: ChatId, agent_id: str, watcher: AgentSessionWatcher) -> int:
        """Freeze everything ``watcher`` can still serve for ``agent_id``, and return the event count.

        Called while the agent is alive and frozen, so the transcript cannot grow
        under the capture and every detail payload is still on disk. A detail
        fetch that fails is recorded as absent rather than aborting the capture:
        losing one tool call's payload is a far smaller loss than refusing the
        switch, and the event itself (with its labels and prose) still renders.
        """
        rows: list[dict[str, Any]] = []
        for event in watcher.get_all_events():
            detail = None
            event_id = event.get("event_id")
            if isinstance(event_id, str):
                detail = watcher.get_event_detail(event_id)
            rows.append({_EVENT_KEY: event, _DETAIL_KEY: detail})
        with self._lock:
            path = self._segment_path(chat_id, agent_id)
            if path is None:
                self._in_memory[self._segment_key(chat_id, agent_id)] = rows
                return len(rows)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                # Atomic because a crash mid-capture must leave the switch's pre-commit
                # state (no archive at all) rather than a truncated transcript that
                # would look complete to every later read.
                write_json_atomic(path, "".join(f"{json.dumps(row)}\n" for row in rows))
            except OSError as e:
                _loguru_logger.opt(exception=e).warning("Failed to archive the transcript of agent {}", agent_id)
                return 0
        return len(rows)

    def load(self, chat_id: ChatId, agent_id: str) -> list[dict[str, Any]]:
        """One retired segment's archived rows, oldest first. Empty when nothing was archived."""
        with self._lock:
            path = self._segment_path(chat_id, agent_id)
            if path is None:
                return list(self._in_memory.get(self._segment_key(chat_id, agent_id), ()))
            if not path.is_file():
                return []
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as e:
                _loguru_logger.opt(exception=e).warning("Failed to read the archived transcript at {}", path)
                return []
        rows: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError as e:
                _loguru_logger.opt(exception=e).warning("Ignoring an unreadable archived event in {}", path)
        return rows

    def remove_chat(self, chat_id: ChatId) -> None:
        """Drop every archived segment of a deleted chat."""
        with self._lock:
            prefix = f"{chat_id}."
            for key in [key for key in self._in_memory if key.startswith(prefix)]:
                del self._in_memory[key]
            if self.archives_dir is None or not self.archives_dir.is_dir():
                return
            for path in self.archives_dir.glob(f"{chat_id}.*.jsonl"):
                try:
                    path.unlink(missing_ok=True)
                except OSError as e:
                    _loguru_logger.opt(exception=e).warning("Failed to remove the archived transcript at {}", path)


class ArchivedSegmentWatcher(StoreBackedWatcher):
    """Serves one retired segment's frozen transcript through the live watcher interface.

    A :class:`StoreBackedWatcher` with nothing to watch: the rows are ingested
    once into a single lane and never change, so there are no paths, the refresh
    is a no-op after the first fill, and ``start``/``stop`` do nothing beyond it.
    Being a watcher rather than a bespoke reader is the point -- the composite
    below, and through it every read endpoint, treats an archived segment and a
    live agent identically.
    """

    _rows: tuple[dict[str, Any], ...]
    _is_filled: bool
    _details: dict[str, dict[str, Any]]

    @classmethod
    def build_from_rows(cls, agent_id: str, rows: list[dict[str, Any]]) -> "ArchivedSegmentWatcher":
        watcher = cls()
        watcher._init_store_watcher(agent_id, lambda _agent_id, _events: None)
        watcher._rows = tuple(rows)
        watcher._is_filled = False
        watcher._details = {}
        return watcher

    @classmethod
    def build(cls, agent_info: AgentInfo, on_events: OnEventsCallback) -> "ArchivedSegmentWatcher":
        """Unreachable: an archived segment is built from rows, never from a live agent."""
        raise NotImplementedError("ArchivedSegmentWatcher is built with build_from_rows")

    def _watch_paths(self) -> tuple[Path, ...]:
        return ()

    def _refresh_locked(self) -> None:
        if self._is_filled:
            return
        self._is_filled = True
        self._store.ensure_lane(self._agent_id)
        for row in self._rows:
            event = row.get(_EVENT_KEY)
            if not isinstance(event, dict):
                continue
            event_id = event.get("event_id")
            # The store indexes by event_id and would raise without one. A row that
            # malformed can only come from a hand-edited or truncated archive file,
            # and dropping it beats refusing to serve the rest of the history.
            if not isinstance(event_id, str):
                continue
            self._store.ingest(self._agent_id, event)
            detail = row.get(_DETAIL_KEY)
            if isinstance(detail, dict):
                self._details[event_id] = detail

    def start(self) -> None:
        """Fill the store once, and start no watch loop.

        Overridden rather than inherited: the base class would spin up a
        :class:`PathWatcher` over ``_watch_paths()``, and a frozen transcript has
        nothing that can change, so the thread would only ever cost memory.
        """
        self._prime()

    def stop(self) -> None:
        return None

    def get_event_detail(self, event_id: str) -> dict[str, Any] | None:
        """The payload captured at retirement, not a disk read.

        The base class re-reads a byte range from the agent's transcript, which is
        exactly what no longer exists here -- hence the inline capture.
        """
        with self._lock:
            self._refresh_locked()
            return self._details.get(event_id)


class CompositeChatWatcher(AgentSessionWatcher):
    """One chat's whole transcript: its retired segments, then its live agent.

    Reads address a single continuous conversation, so this presents the
    concatenation as one transcript: offsets are global across segments, paging
    walks off the end of one segment into the next, and ``total`` is the sum.
    Everything live -- the watch loop, the SSE fan-out, the queue and flush hooks
    -- belongs to the active agent alone and is delegated straight through; the
    archived segments are immutable history and generate no events.
    """

    def __init__(self, archived: tuple[ArchivedSegmentWatcher, ...], live: AgentSessionWatcher) -> None:
        self._archived = archived
        self._live = live

    @classmethod
    def build(cls, agent_info: AgentInfo, on_events: OnEventsCallback) -> "CompositeChatWatcher":
        """Unreachable: a composite wraps watchers that already exist."""
        raise NotImplementedError("CompositeChatWatcher is built from existing watchers")

    @property
    def live(self) -> AgentSessionWatcher:
        """The active agent's own watcher, for callers that mean the agent and not the chat."""
        return self._live

    def _segments(self) -> tuple[AgentSessionWatcher, ...]:
        return (*self._archived, self._live)

    def start(self) -> None:
        for segment in self._segments():
            segment.start()

    def stop(self) -> None:
        for segment in self._segments():
            segment.stop()

    # -- reads: archived history first, then the live tail ------------------------------

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for segment in self._segments():
            events.extend(segment.get_all_events(session_id))
        return events

    def get_total_event_count(self, session_id: str | None = None) -> int:
        return sum(segment.get_total_event_count(session_id) for segment in self._segments())

    def get_tail_events(self, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        """The newest ``limit`` events, reaching back into archived history when the live
        agent is younger than the window (the first load right after a switch)."""
        collected: list[dict[str, Any]] = []
        for segment in reversed(self._segments()):
            remaining = limit - len(collected)
            if remaining <= 0:
                break
            collected = segment.get_tail_events(remaining, session_id) + collected
        return collected

    def get_events_at_offset(self, offset: int, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        """A window at a GLOBAL offset, which may straddle a segment boundary."""
        collected: list[dict[str, Any]] = []
        cursor = max(offset, 0)
        for segment in self._segments():
            if len(collected) >= limit:
                break
            total = segment.get_total_event_count(session_id)
            if cursor >= total:
                cursor -= total
                continue
            collected.extend(segment.get_events_at_offset(cursor, limit - len(collected), session_id))
            cursor = 0
        return collected

    def get_event_offset(self, event_id: str, session_id: str | None = None) -> int:
        """The event's index in the WHOLE chat, so a window from any segment places itself
        against the chat's length rather than its own segment's. -1 when no segment holds
        it, which is the interface's existing not-found answer."""
        base = 0
        for segment in self._segments():
            offset = segment.get_event_offset(event_id, session_id)
            if offset >= 0:
                return base + offset
            base += segment.get_total_event_count(session_id)
        return -1

    def get_backfill_events(
        self, before_event_id: str, limit: int = 50, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Page older across the boundary: what the cursor's own segment can supply,
        topped up from the segments before it (which is how the very first scroll back
        past a switch reaches the previous harness's turns)."""
        segments = self._segments()
        index = self._index_of(segments, before_event_id, session_id)
        if index is None:
            return []
        collected = segments[index].get_backfill_events(before_event_id, limit, session_id)
        for earlier in reversed(segments[:index]):
            if len(collected) >= limit:
                break
            collected = earlier.get_tail_events(limit - len(collected), session_id) + collected
        return collected

    def get_forward_events(
        self, after_event_id: str, limit: int = 50, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Page newer across the boundary, the mirror of ``get_backfill_events``."""
        segments = self._segments()
        index = self._index_of(segments, after_event_id, session_id)
        if index is None:
            return []
        collected = segments[index].get_forward_events(after_event_id, limit, session_id)
        for later in segments[index + 1 :]:
            if len(collected) >= limit:
                break
            collected.extend(later.get_events_at_offset(0, limit - len(collected), session_id))
        return collected

    def get_event_detail(self, event_id: str) -> dict[str, Any] | None:
        for segment in self._segments():
            detail = segment.get_event_detail(event_id)
            if detail is not None:
                return detail
        return None

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        for segment in self._segments():
            metadata = segment.get_subagent_metadata(subagent_session_id)
            if metadata is not None:
                return metadata
        return None

    @staticmethod
    def _index_of(segments: tuple[AgentSessionWatcher, ...], event_id: str, session_id: str | None) -> int | None:
        """Which segment holds ``event_id``, asked through ``get_event_offset``.

        Its -1-for-missing contract is the ownership test, so locating a paging
        cursor needs no new method on the watcher interface.
        """
        for index, segment in enumerate(segments):
            if segment.get_event_offset(event_id, session_id) >= 0:
                return index
        return None

    # -- live-only surface: the active agent owns all of it -----------------------------

    def is_main_session_event(self, event: dict[str, Any]) -> bool:
        return self._live.is_main_session_event(event)

    def set_queue_snapshot_callback(self, callback: QueueSnapshotCallback) -> None:
        self._live.set_queue_snapshot_callback(callback)

    def get_queued_messages(self) -> list[dict[str, Any]]:
        return self._live.get_queued_messages()

    def get_latest_main_session_file(self) -> Path | None:
        return self._live.get_latest_main_session_file()

    def get_queued_block(self) -> str:
        return self._live.get_queued_block()

    def clear_queue(self) -> None:
        self._live.clear_queue()

    def notify_idle(self) -> list[dict[str, Any]]:
        return self._live.notify_idle()

    def take_unclaimed_queue(self) -> tuple[str, tuple[str, ...]]:
        return self._live.take_unclaimed_queue()

    def take_whole_queue(self) -> tuple[str, tuple[str, ...]]:
        return self._live.take_whole_queue()

    def claim_queue_for_tap(self) -> tuple[str, tuple[str, ...], int]:
        return self._live.claim_queue_for_tap()

    def release_tap_claim(self, claimed: tuple[str, ...], generation: int) -> None:
        self._live.release_tap_claim(claimed, generation)

    def set_flush_hooks(self, send: FlushSendCallback, is_alive: IsAliveCallback) -> None:
        self._live.set_flush_hooks(send, is_alive)


def build_chat_watcher(
    chat_id: ChatId,
    retired_agent_ids: tuple[str, ...],
    live: AgentSessionWatcher,
    archive: TranscriptArchive,
) -> AgentSessionWatcher:
    """The watcher for a chat: ``live`` alone, or a composite when it has history behind it.

    Returns ``live`` unchanged for a chat that has never switched harness, which
    is every chat until it does -- so the composite is not on the path of a
    workspace that never uses the feature. A retired segment whose archive is
    missing (a capture that failed, a workspace restored without it) is skipped:
    a chat that lost part of its history must still serve the rest.
    """
    archived: list[ArchivedSegmentWatcher] = []
    for agent_id in retired_agent_ids:
        rows = archive.load(chat_id, agent_id)
        if not rows:
            continue
        watcher = ArchivedSegmentWatcher.build_from_rows(agent_id, rows)
        watcher.start()
        archived.append(watcher)
    if not archived:
        return live
    return CompositeChatWatcher(tuple(archived), live)

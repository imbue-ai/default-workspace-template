"""Read a room's transcript into page events, and tail it for new ones.

Built on the shared :class:`~imbue.chat.harnesses.transcript_store` scaffolding with a single lane: the room's
one file, appended to by the chat app and by the room's reader (``room/transcript.py``), consumed incrementally
by byte offset like pi's live session file. :class:`RoomTranscriptLoader` is the read half, which an archived
segment of a chat that was once a room reads through; :class:`RoomSessionWatcher` adds the live half.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.room.transcript import step_as_page_event
from imbue.chat.harnesses.room.transcript import transcript_path
from imbue.chat.harnesses.session_watcher import OnEventsCallback
from imbue.chat.harnesses.transcript_store import StoreBackedTranscriptLoader
from imbue.chat.harnesses.transcript_store import StoreBackedWatcher
from imbue.chat.harnesses.transcript_store import iter_line_spans
from imbue.chat.harnesses.transcript_store import split_at_last_complete_line

_LANE = "main"


class RoomTranscriptLoader(StoreBackedTranscriptLoader):
    """Reads a room's transcript file into the store, incrementally."""

    _path: Path
    _byte_offset: int

    @classmethod
    def build_loader(cls, agent_info: AgentInfo) -> RoomTranscriptLoader:
        loader = cls.__new__(cls)
        loader._init_loader(agent_info.id)
        loader._init_room_state(agent_info.agent_state_dir)
        return loader

    def _init_room_state(self, agent_state_dir: Path) -> None:
        self._path = transcript_path(agent_state_dir)
        self._byte_offset = 0

    def _refresh_locked(self) -> None:
        try:
            size = self._path.stat().st_size
        except OSError:
            return
        # The transcript is append-only; a shrink is a rewrite, re-read from the start (the store's id-based dedup
        # drops the copies).
        if size < self._byte_offset:
            self._byte_offset = 0
        if size == self._byte_offset:
            return
        try:
            with self._path.open("rb") as transcript:
                transcript.seek(self._byte_offset)
                raw = transcript.read()
        except OSError:
            logger.debug("room watcher: failed to read {}", self._path)
            return
        complete, _fragment = split_at_last_complete_line(raw)
        if not complete:
            return
        for byte_offset, byte_len, line_bytes in iter_line_spans(complete, self._byte_offset):
            stripped = line_bytes.decode("utf-8", errors="replace").strip()
            if not stripped:
                continue
            event = self._adapt_line(stripped)
            if event is not None:
                self._ingest_locked(_LANE, event, (self._path, byte_offset, byte_len))
        self._byte_offset += len(complete)

    def _adapt_line(self, line: str) -> dict[str, Any] | None:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as e:
            logger.warning("room watcher: skipping a malformed transcript line: {}", e)
            return None
        if not isinstance(record, dict):
            return None
        return step_as_page_event(record)


class RoomSessionWatcher(RoomTranscriptLoader, StoreBackedWatcher):
    """Watches a room's transcript file and emits each new message as it lands."""

    @classmethod
    def build(cls, agent_info: AgentInfo, on_events: OnEventsCallback) -> RoomSessionWatcher:
        watcher = cls.__new__(cls)
        watcher._init_store_watcher(agent_info.id, on_events)
        watcher._init_room_state(agent_info.agent_state_dir)
        return watcher

    def _watch_paths(self) -> tuple[Path, ...]:
        return (self._path,)

"""Shared interface and construction point for the two transcript watchers.

``AgentSessionWatcher`` (Claude, parsed from the raw session JSONL) and
``CommonTranscriptWatcher`` (the other three harnesses, via mngr's shared
transcript schema) are read from identically by ``server.py`` and
``app_context.py``. ``TranscriptWatcher`` is the contract between them, so
call sites can hold either without an isinstance check; ``build_transcript_watcher``
is the one place that decides which implementation an agent gets, so that
decision is made once rather than re-derived at each construction site.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from typing import Protocol
from typing import runtime_checkable

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.common_transcript_watcher import CommonTranscriptWatcher
from imbue.system_interface.harness import Harness
from imbue.system_interface.session_watcher import AgentSessionWatcher


@runtime_checkable
class TranscriptWatcher(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]: ...

    def get_tail_events(self, limit: int, session_id: str | None = None) -> list[dict[str, Any]]: ...

    def get_backfill_events(
        self, before_event_id: str, limit: int = 50, session_id: str | None = None
    ) -> list[dict[str, Any]]: ...

    def get_forward_events(
        self, after_event_id: str, limit: int = 50, session_id: str | None = None
    ) -> list[dict[str, Any]]: ...

    def get_events_at_offset(self, offset: int, limit: int, session_id: str | None = None) -> list[dict[str, Any]]: ...

    def get_event_offset(self, event_id: str, session_id: str | None = None) -> int: ...

    def get_total_event_count(self, session_id: str | None = None) -> int: ...

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None: ...

    def is_main_session_event(self, event: dict[str, Any]) -> bool: ...


def build_transcript_watcher(
    agent_info: AgentInfo, on_events: Callable[[str, list[dict[str, Any]]], None]
) -> TranscriptWatcher:
    """Construct the right watcher for ``agent_info.harness``.

    Claude, and an unrecognized/``None`` harness (e.g. a custom agent type
    that predates this routing), get ``AgentSessionWatcher`` -- the existing
    behavior, and the only implementation that knows how to read a raw
    Claude session file. The other three known harnesses have no raw format
    worth a bespoke parser (see ``common_transcript_watcher.py``) and get
    ``CommonTranscriptWatcher`` instead.
    """
    if agent_info.harness is None or agent_info.harness is Harness.CLAUDE:
        return AgentSessionWatcher(
            agent_id=agent_info.id,
            agent_state_dir=agent_info.agent_state_dir,
            claude_config_dir=agent_info.claude_config_dir,
            on_events=on_events,
        )
    return CommonTranscriptWatcher(agent_id=agent_info.id, on_events=on_events)

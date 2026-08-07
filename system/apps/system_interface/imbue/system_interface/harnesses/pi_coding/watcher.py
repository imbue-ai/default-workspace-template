"""Placeholder transcript watcher for pi agents.

This first cut wires pi into the UI for the **model bar** only -- it does NOT yet
render pi's conversation. So the watcher is a no-op that satisfies the
:class:`AgentSessionWatcher` contract with an empty transcript: the pi tab shows no
messages until the real watcher (tailing pi's native session JSONL via the
``pi_session_file`` marker, mirroring codex) replaces this. Everything else -- the
model bar's catalog and live ``model_choice`` -- works regardless, since those flow
through the resolver, not the watcher.
"""

from __future__ import annotations

from typing import Any
from typing import Callable

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher
from imbue.system_interface.harnesses.session_watcher import OnEventsCallback


class PiPlaceholderSessionWatcher(AgentSessionWatcher):
    """A watcher that emits nothing -- pi's transcript is not wired up yet."""

    _agent_id: str
    _on_events: Callable[[str, list[dict[str, Any]]], None]

    @classmethod
    def build(cls, agent_info: AgentInfo, on_events: OnEventsCallback) -> "PiPlaceholderSessionWatcher":
        self = cls.__new__(cls)
        self._agent_id = agent_info.id
        self._on_events = on_events
        return self

    def start(self) -> None:
        """No-op: nothing is tailed yet."""

    def stop(self) -> None:
        """No-op."""

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_tail_events(self, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_backfill_events(
        self, before_event_id: str, limit: int, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        return []

    def get_forward_events(
        self, after_event_id: str, limit: int, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        return []

    def get_events_at_offset(self, offset: int, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_event_offset(self, event_id: str, session_id: str | None = None) -> int:
        return -1

    def get_total_event_count(self, session_id: str | None = None) -> int:
        return 0

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        return None

    def is_main_session_event(self, event: dict[str, Any]) -> bool:
        return True

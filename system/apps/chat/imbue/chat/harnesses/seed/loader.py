"""The reader of a chat's seed segment: the turns the Mind app handed over, with nothing watching them.

A seed segment never changes once written, so this is a loader primed at build. It is also
registered as the ``seed`` harness's watcher because the registry requires one per harness;
as a watcher it watches nothing, like the placeholder harnesses' (``harnesses/placeholder``),
since no agent ever runs on the seed harness.
"""

from typing import Any

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.chat_seed import read_seed_events
from imbue.chat.harnesses.session_watcher import AgentSessionWatcher
from imbue.chat.harnesses.session_watcher import OnEventsCallback
from imbue.chat.harnesses.session_watcher import TranscriptLoader


class SeedSessionWatcher(AgentSessionWatcher, TranscriptLoader):
    """The seed segment's events, resident from build, answering every read from the list."""

    _events: list[dict[str, Any]]
    _offset_by_event_id: dict[str, int]

    @classmethod
    def build(cls, agent_info: AgentInfo, on_events: OnEventsCallback) -> "SeedSessionWatcher":
        watcher = cls.__new__(cls)
        # The pseudo-agent's state dir is the chat's own folder, where the seed file lives.
        watcher._events = read_seed_events(agent_info.agent_state_dir)
        watcher._offset_by_event_id = {event["event_id"]: index for index, event in enumerate(watcher._events)}
        return watcher

    @classmethod
    def build_loader(cls, agent_info: AgentInfo) -> "SeedSessionWatcher":
        return cls.build(agent_info, lambda _agent_id, _events: None)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return list(self._events)

    def get_tail_events(self, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        return self._events[-limit:]

    def get_backfill_events(
        self, before_event_id: str, limit: int, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        end = self._offset_by_event_id.get(before_event_id)
        if end is None or limit <= 0:
            return []
        return self._events[max(0, end - limit) : end]

    def get_forward_events(
        self, after_event_id: str, limit: int, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        start = self._offset_by_event_id.get(after_event_id)
        if start is None or limit <= 0:
            return []
        return self._events[start + 1 : start + 1 + limit]

    def get_events_at_offset(self, offset: int, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        start = max(0, offset)
        return self._events[start : start + limit]

    def get_event_offset(self, event_id: str, session_id: str | None = None) -> int:
        return self._offset_by_event_id.get(event_id, -1)

    def get_total_event_count(self, session_id: str | None = None) -> int:
        return len(self._events)

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        return None

    def is_main_session_event(self, event: dict[str, Any]) -> bool:
        return True

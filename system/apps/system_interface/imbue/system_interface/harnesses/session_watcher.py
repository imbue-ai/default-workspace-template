"""The per-agent transcript watcher interface every harness implements.

One watcher instance tails ONE agent's transcript, whatever form that takes -- claude's
rotating session JSONL files, codex's single rollout -- parses new lines into the common
event schema (see :mod:`harnesses.events`), and hands them to ``on_events``. Everything
downstream of that callback is harness-blind: the server's read endpoints and the
activity tracker both work against this interface, never a concrete watcher.

``build`` takes the whole :class:`AgentInfo` rather than individual paths on purpose.
Harnesses need different pieces of it -- claude reads ``claude_config_dir``, codex does
not -- so passing individual arguments would force the CALLER to know which harness needs
what, which is exactly the knowledge this interface removes. Handing over the record lets
each implementation take what it needs and leaves ``app_context`` free of harness names.

Adding a harness is a new subclass plus one entry in ``harnesses.registry``; no edits
here and none in the caller.
"""

from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from typing import Any

from imbue.system_interface.agent_discovery import AgentInfo

# Called with (agent_id, newly parsed events) each time the watcher reads new lines.
# Fanned out to the event queues (and so to the browser) and to the activity tracker.
OnEventsCallback = Callable[[str, list[dict[str, Any]]], None]

# Called with the full queued-message snapshot each time it changes. Harness-
# agnostic wire shape (a list of ``{queued_id, content, timestamp}`` dicts); the
# only harness-specific code is the populator that produces it (see
# ``harnesses.claude.queue_tracker``).
QueueSnapshotCallback = Callable[[list[dict[str, Any]]], None]


class AgentSessionWatcher(ABC):
    """Watches one agent's transcript and emits parsed events."""

    @classmethod
    @abstractmethod
    def build(cls, agent_info: AgentInfo, on_events: OnEventsCallback) -> "AgentSessionWatcher":
        """Construct a watcher for ``agent_info``, not yet started."""

    @abstractmethod
    def start(self) -> None:
        """Begin watching. Idempotent."""

    @abstractmethod
    def stop(self) -> None:
        """Stop watching and release the observer. Idempotent."""

    @abstractmethod
    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Every event parsed so far, in chronological order."""

    @abstractmethod
    def get_tail_events(self, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        """The newest ``limit`` events."""

    @abstractmethod
    def get_backfill_events(
        self, before_event_id: str, limit: int, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Up to ``limit`` events immediately preceding ``before_event_id``."""

    @abstractmethod
    def get_forward_events(
        self, after_event_id: str, limit: int, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Up to ``limit`` events immediately following ``after_event_id``."""

    @abstractmethod
    def get_events_at_offset(self, offset: int, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        """``limit`` events starting at ``offset`` from the beginning."""

    @abstractmethod
    def get_event_offset(self, event_id: str, session_id: str | None = None) -> int:
        """The index of ``event_id``, or -1 when it is not present."""

    @abstractmethod
    def get_total_event_count(self, session_id: str | None = None) -> int:
        """How many events have been parsed."""

    @abstractmethod
    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        """Metadata for a subagent's session, or None when the harness has no subagents."""

    @abstractmethod
    def is_main_session_event(self, event: dict[str, Any]) -> bool:
        """True when ``event`` belongs to the agent's own session rather than a subagent's."""

    # --- Queued messages (the shoulder-tap surface). ---------------------------
    # Concrete no-op defaults so a harness without a queued-message populator
    # needs no changes; the Claude watcher overrides them. Everything downstream
    # (the WS snapshot field and the two common actions) is harness-agnostic.

    def set_queue_snapshot_callback(self, callback: QueueSnapshotCallback) -> None:
        """Register the sink that receives each new queued-message snapshot. No-op by default."""

    def get_queued_messages(self) -> list[dict[str, Any]]:
        """The current queued-message snapshot; empty for a harness with no populator."""
        return []

    def get_queued_block(self) -> str:
        """The queued messages as one concatenated turn; empty for a harness with no populator."""
        return ""

    def clear_queue(self) -> None:
        """Drop the queued set (after a flush restart). No-op for a harness with no populator."""

    def notify_idle(self) -> list[dict[str, Any]]:
        """Apply the working->IDLE backstop and return the resulting snapshot (empty by default)."""
        return []

    def note_sent_message(self, content: str, timestamp: str) -> str | None:
        """The UI is about to send ``content`` to this agent (called BEFORE the send).

        No-op returning None by default. A harness whose enqueue source is the send
        itself (one with no on-disk enqueue ledger) overrides this to park the
        message write-ahead -- before delivery, so its own drain can never race past the
        record -- and returns an opaque token for :meth:`retract_sent_message`.
        """
        return None

    def retract_sent_message(self, token: str) -> None:
        """The send behind ``token`` failed: un-park it (compensation for write-ahead).
        No-op by default."""

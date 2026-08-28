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
from pathlib import Path
from typing import Any

from imbue.system_interface.agent_discovery import AgentInfo

# Called with (agent_id, newly parsed events) each time the watcher reads new lines.
# Fanned out to the event queues (and so to the browser) and to the activity tracker.
OnEventsCallback = Callable[[str, list[dict[str, Any]]], None]

# Sends one message to the harness, returning whether it was accepted. Bound by the
# composition root to the manager's send path (which resolves the agent's location).
FlushSendCallback = Callable[[str], bool]

# Whether the agent's process is currently alive -- see ``set_flush_hooks``.
IsAliveCallback = Callable[[], bool]

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

    def get_latest_main_session_file(self) -> Path | None:
        """The live process's (latest main) session file, for the native tap's byte baseline.

        None by default (a harness whose tap needs no on-disk session anchor -- codex writes a
        control line, pi an inbox sentinel). The Claude watcher overrides it: its tap cancels
        the live turn and reads the raw post-chord tail from this file.
        """
        return None

    def get_queued_block(self) -> str:
        """The queued messages as one concatenated turn; empty for a harness with no populator."""
        return ""

    def clear_queue(self) -> None:
        """Drop the queued set (after a flush restart). No-op for a harness with no populator."""

    def notify_idle(self) -> list[dict[str, Any]]:
        """Apply the working->IDLE backstop and return the resulting snapshot (empty by default)."""
        return []

    def take_unclaimed_queue(self) -> tuple[str, tuple[str, ...]]:
        """Remove and return the queue entries no delivery has claimed, as one block.

        Stop's return path, for a harness that holds the queue itself. It exists instead of
        ``clear_queue`` because clearing cannot distinguish the entries stop accounted for from
        ones the user sent while the cancel was settling, and wiping the latter leaves them in
        no state at all (contract A1). Entries a delivery HAS claimed are deliberately left:
        that send may still land, and returning them too would make one message both Delivered
        and Returned. Empty by default.
        """
        return "", ()

    def take_whole_queue(self) -> tuple[str, tuple[str, ...]]:
        """Remove and return every queue entry, claimed included -- the restart path.

        A restart kills the agent process, so an in-flight send dies with it and its entries
        can be returned without risking a double. Taking them is mandatory: the shared restart
        drain clears the queue as it goes, so anything left is destroyed unaccounted. Empty by
        default.
        """
        return "", ()

    def claim_queue_for_tap(self) -> tuple[str, tuple[str, ...], int]:
        """Claim the queue on a shoulder-tap's behalf: returns (block, claimed ids, generation).

        Claiming is what greys the tap button for the duration of the tap's own run, so a
        second tap cannot arrive and deliver a second cancel key. Empty by default.
        """
        return "", (), 0

    def release_tap_claim(self, claimed: tuple[str, ...], generation: int) -> None:
        """Hand a tap's claim back unsettled, without charging a delivery attempt. No-op by default."""

    def set_flush_hooks(self, send: "FlushSendCallback", is_alive: "IsAliveCallback") -> None:
        """Give a watcher the two capabilities it needs to DELIVER its own queue. No-op by default.

        Only a harness that holds the queue on the agent's behalf needs these -- antigravity,
        whose parked messages live invisibly inside its TUI, so nothing else can deliver them.
        Every other harness's queue is consumed by the harness itself and needs no sender.

        ``is_alive`` is not optional politeness: mngr's text send AUTO-STARTS a stopped agent,
        and the working->IDLE signal cannot distinguish "turn finished" from "process died". A
        flush that skipped this check would resurrect a stopped agent and deliver its queue,
        which the contract forbids outright ("NEVER auto-sent on resume"; "the queue is empty
        whenever the agent is stopped").
        """

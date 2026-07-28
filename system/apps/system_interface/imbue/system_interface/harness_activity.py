"""Per-agent, per-harness activity tracking.

One tracker instance holds ONE agent's cached transcript signals and knows how to
turn them into an :class:`ActivityState`. ``AgentManager`` owns a tracker per
tracked agent (built from the agent's ``harness``) and calls it instead of
branching on the harness itself, so the harness-specific pieces -- which signals
are cached, how they derive, and which ``*_process_started`` marker bounds
staleness -- all live together in one class per harness.

Adding a harness is a new subclass plus one entry in ``TRACKER_BY_HARNESS``; no
edits to ``AgentManager``.

The derivation itself stays in the pure peers (:mod:`claude_activity_state`,
:mod:`codex_activity_state`) -- these classes only cache signals and dispatch.

Thread-safety: a tracker holds plain mutable state and takes no lock of its own.
``AgentManager`` mutates it under ``AgentManager._lock``, exactly as it did the
per-agent dicts these replace. ``marker_filename`` is a class constant, so it is
the one member safe to read without the lock (``_read_process_started_at`` stats
the marker outside the lock on purpose, to keep a filesystem call off the hot
path).
"""

from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from typing import Any
from typing import ClassVar

from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.activity_state import has_unmatched_tool_use
from imbue.system_interface.activity_state import last_event_timestamp
from imbue.system_interface.activity_state import last_event_type
from imbue.system_interface.activity_state import parse_iso_timestamp_to_epoch
from imbue.system_interface.claude_activity_state import derive_claude
from imbue.system_interface.codex_activity_state import codex_turn_open
from imbue.system_interface.codex_activity_state import derive_codex


class HarnessActivityTracker(ABC):
    """One agent's cached activity signals, for one harness.

    Subclasses declare their ``*_process_started`` marker, cache whichever
    transcript signals their derivation needs, and implement :meth:`observe` /
    :meth:`derive`.
    """

    # Filename of the marker mngr touches on every startup/resume for this
    # harness. Its mtime bounds transcript staleness, so it MUST match what the
    # harness's mngr plugin writes (mngr_claude -> claude_process_started,
    # mngr_codex -> codex_process_started).
    marker_filename: ClassVar[str]

    # Signals every harness caches. Declared at class level so a `build()`
    # classmethod (no __init__) can assign them with the type checker happy.
    _has_pending_tool_use: bool
    _last_event_type: str | None
    _last_event_timestamp: str | None

    @classmethod
    def build(cls) -> "HarnessActivityTracker":
        tracker = cls.__new__(cls)
        tracker.reset()
        return tracker

    def reset(self) -> None:
        """Clear every cached signal so the next :meth:`derive` settles on IDLE.

        Used both to initialize and to force an agent back to IDLE after an
        interrupt/restart, which abandons a transcript mid-turn: the tail is
        still an unmatched ``tool_use`` (or, for codex, an unclosed turn) that
        no later transcript event will close, so the backend must drop the
        cached signals explicitly.
        """
        self._has_pending_tool_use = False
        self._last_event_type = None
        self._last_event_timestamp = None

    @property
    def _tail_event_at(self) -> float | None:
        """The cached tail event's timestamp as epoch seconds, or None."""
        return parse_iso_timestamp_to_epoch(self._last_event_timestamp)

    @abstractmethod
    def observe(self, events: Sequence[dict[str, Any]]) -> bool:
        """Refresh cached signals from the agent's full event list.

        Returns True iff a signal that affects derivation changed -- callers use
        that to skip an otherwise-pointless recompute (and its marker ``stat``)
        for streamed events that move nothing.
        """

    @abstractmethod
    def derive(self, *, is_agent_running: bool, process_started_at: float | None) -> ActivityState:
        """Derive this agent's activity state from the cached signals.

        ``process_started_at`` is the mtime of :attr:`marker_filename`, or None
        when the marker is absent.
        """


class ClaudeActivityTracker(HarnessActivityTracker):
    """Claude: no turn markers in the transcript, so activity is inferred from
    the mngr lifecycle plus the transcript tail. See :func:`derive_claude`."""

    marker_filename: ClassVar[str] = "claude_process_started"

    def observe(self, events: Sequence[dict[str, Any]]) -> bool:
        new_pending = has_unmatched_tool_use(events)
        new_last_type = last_event_type(events)
        if new_pending == self._has_pending_tool_use and new_last_type == self._last_event_type:
            return False
        self._has_pending_tool_use = new_pending
        self._last_event_type = new_last_type
        # Refreshed alongside the type so the stale-tail check sees the current
        # tail's time. Deliberately inside the change guard: an event that moves
        # no derived signal leaves the timestamp alone and skips the recompute.
        self._last_event_timestamp = last_event_timestamp(events)
        return True

    def derive(self, *, is_agent_running: bool, process_started_at: float | None) -> ActivityState:
        return derive_claude(
            is_agent_running=is_agent_running,
            has_pending_tool_use=self._has_pending_tool_use,
            tail_event_type=self._last_event_type,
            tail_event_at=self._tail_event_at,
            process_started_at=process_started_at,
        )


class CodexActivityTracker(HarnessActivityTracker):
    """Codex: the transcript carries authoritative turn boundaries, so activity
    is a latch on those rather than a lifecycle heuristic. See :func:`derive_codex`."""

    marker_filename: ClassVar[str] = "codex_process_started"

    # Whether the latest turn marker is an open turn (``turn_started`` with no
    # ``turn_completed``/``turn_aborted`` after it).
    _turn_open: bool

    def reset(self) -> None:
        super().reset()
        self._turn_open = False

    def observe(self, events: Sequence[dict[str, Any]]) -> bool:
        new_pending = has_unmatched_tool_use(events)
        new_turn_open = codex_turn_open(events)
        new_last_type = last_event_type(events)
        if (
            new_pending == self._has_pending_tool_use
            and new_turn_open == self._turn_open
            and new_last_type == self._last_event_type
        ):
            return False
        self._has_pending_tool_use = new_pending
        self._turn_open = new_turn_open
        self._last_event_type = new_last_type
        self._last_event_timestamp = last_event_timestamp(events)
        return True

    def derive(self, *, is_agent_running: bool, process_started_at: float | None) -> ActivityState:
        # The mngr lifecycle is unreliable for codex, so `is_agent_running` is
        # intentionally unused here -- the turn latch is authoritative.
        return derive_codex(
            turn_open=self._turn_open,
            has_pending_tool_use=self._has_pending_tool_use,
            tail_event_at=self._tail_event_at,
            process_started_at=process_started_at,
        )


# One entry per harness. Keyed by mngr's ``AgentDetails.type``, which the
# system interface carries end-to-end as ``harness`` (set at creation, confirmed
# by discovery) -- never sniffed from the agent's files.
TRACKER_BY_HARNESS: dict[str, type[HarnessActivityTracker]] = {
    "claude": ClaudeActivityTracker,
    "codex": CodexActivityTracker,
}


def build_tracker_for_harness(harness: str) -> HarnessActivityTracker:
    """Build the activity tracker for ``harness``.

    An unregistered harness falls back to the claude tracker, matching the
    previous ``if harness == "codex" ... else claude`` dispatch: mngr agent types
    with no tracker of their own (e.g. ``wait``) still get lifecycle-plus-tail
    derivation rather than no activity indicator at all.
    """
    return TRACKER_BY_HARNESS.get(harness, ClaudeActivityTracker).build()

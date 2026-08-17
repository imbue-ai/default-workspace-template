"""Per-agent, per-harness activity tracking.

One tracker instance holds ONE agent's cached transcript signals and knows how to
turn them into an :class:`ActivityState`. ``AgentManager`` owns a tracker per
tracked agent (built from the agent's ``harness``) and calls it instead of
branching on the harness itself, so the harness-specific pieces -- which signals
are cached, how they derive, and which ``*_process_started`` marker bounds
staleness -- all live together in one class per harness.

Adding a harness is a new subclass plus one entry in ``harnesses.registry``; no
edits to ``AgentManager``.

The derivation itself stays in each harness's pure ``activity_state`` peer -- these
classes only cache signals and dispatch.

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


class HarnessActivityTracker(ABC):
    """One agent's cached activity signals, for one harness.

    Subclasses declare their ``*_process_started`` marker, cache whichever
    transcript signals their derivation needs, and implement :meth:`observe` /
    :meth:`derive`.
    """

    # Filename of the marker mngr touches on every startup/resume for this
    # harness. Its mtime bounds transcript staleness, so it MUST match what the
    # harness's mngr plugin writes (mngr_claude -> claude_process_started,
    # mngr_codex -> codex_process_started, mngr_pi_coding -> pi_process_started).
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



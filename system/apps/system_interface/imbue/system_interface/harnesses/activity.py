"""Per-agent, per-harness activity tracking.

One tracker instance holds ONE agent's cached transcript signals and knows how to
turn them into an :class:`ActivityState`. ``AgentManager`` owns a tracker per
tracked agent (built from the agent's ``harness``) and calls it instead of
branching on the harness itself.

The split is per-SIGNAL-CLASS, not per-harness:

- *Is the process alive and is this transcript current?* Universal, so the base
  :meth:`derive` applies the two gates for every harness -- a positively-dead
  lifecycle settles to IDLE (structurally, not by a caller remembering to
  override; codex's own derivation deliberately ignores the lifecycle, so this
  gate is its ONLY dead-process settle), and a transcript tail predating the
  current process's ``*_process_started`` marker is a turn abandoned by a dead
  process, not a live one.
- *Within a live process, is a turn in flight and is a tool running?* The one
  legitimately per-harness question -- :meth:`_derive_working`. claude/pi infer
  it from the lifecycle + ``active`` marker + transcript tail; codex latches its
  transcript's explicit turn markers.

Signal caching (:meth:`observe`) is likewise shared: every parser emits the same
common event schema, so the pending-tool walk and tail bookkeeping are identical;
a harness with extra signals (codex's turn latch) contributes them via
:meth:`_observe_extra`.

Adding a harness is a new subclass plus one entry in ``harnesses.registry``; no
edits to ``AgentManager``.

The per-harness derivations stay in each harness's pure ``activity_state`` peer --
the subclasses only cache signals and dispatch.

Thread-safety: a tracker holds plain mutable state and takes no lock of its own.
``AgentManager`` mutates it under ``AgentManager._lock``. The ClassVars are class
constants, so they are the members safe to read without the lock (the marker
``stat``/``exists`` calls run outside the lock on purpose, to keep filesystem
calls off the hot path).
"""

from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from typing import Any
from typing import ClassVar

from imbue.system_interface.activity_state import ACTIVE_MARKER_FILENAME
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.activity_state import has_unmatched_tool_use
from imbue.system_interface.activity_state import is_lifecycle_dead
from imbue.system_interface.activity_state import is_transcript_tail_stale
from imbue.system_interface.activity_state import last_event_timestamp
from imbue.system_interface.activity_state import last_event_type
from imbue.system_interface.activity_state import parse_iso_timestamp_to_epoch


class HarnessActivityTracker(ABC):
    """One agent's cached activity signals, for one harness.

    Subclasses declare their marker filenames, contribute any extra cached
    signals via :meth:`_observe_extra`, and implement :meth:`_derive_working`.
    """

    # Filename of the marker mngr touches on every startup/resume for this
    # harness. Its mtime bounds transcript staleness, so it MUST match what the
    # harness's mngr plugin writes (mngr_claude -> claude_process_started,
    # mngr_codex -> codex_process_started, mngr_pi_coding -> pi_process_started).
    marker_filename: ClassVar[str]

    # Filename of the turn-in-flight marker the harness's mngr plugin maintains,
    # or None when the harness has no such file. ``None`` is a real declaration
    # ("mngr writes no marker for this harness -- its daemon is the turn
    # authority"), not an escape hatch: codex overrides it, claude/pi keep the
    # shared ``active`` marker their hooks/extension flip.
    active_marker_filename: ClassVar[str | None] = ACTIVE_MARKER_FILENAME

    # Signals every harness caches. Declared at class level so a `build()`
    # classmethod (no __init__) can assign them with the type checker happy.
    _has_pending_tool_use: bool
    _last_event_type: str | None
    _last_event_timestamp: str | None
    # Extra per-harness cached signals (codex's turn latch); () for the rest.
    _extra: tuple[Any, ...]

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
        self._extra = ()

    @property
    def _tail_event_at(self) -> float | None:
        """The cached tail event's timestamp as epoch seconds, or None."""
        return parse_iso_timestamp_to_epoch(self._last_event_timestamp)

    def observe(self, events: Sequence[dict[str, Any]]) -> bool:
        """Refresh cached signals from the agent's full event list.

        Returns True iff a signal that affects derivation changed -- callers use
        that to skip an otherwise-pointless recompute (and its marker ``stat``)
        for streamed events that move nothing. The tail timestamp refreshes
        inside the change guard deliberately: an event that moves no derived
        signal leaves it alone and skips the recompute.
        """
        new_pending = has_unmatched_tool_use(events)
        new_last_type = last_event_type(events)
        new_extra = self._observe_extra(events)
        if (
            new_pending == self._has_pending_tool_use
            and new_last_type == self._last_event_type
            and new_extra == self._extra
        ):
            return False
        self._has_pending_tool_use = new_pending
        self._last_event_type = new_last_type
        self._extra = new_extra
        self._last_event_timestamp = last_event_timestamp(events)
        return True

    def _observe_extra(self, events: Sequence[dict[str, Any]]) -> tuple[Any, ...]:
        """Extra cached signals this harness's derivation needs. Empty by default."""
        return ()

    def derive(
        self, *, lifecycle_state: str, is_active_marker_present: bool, process_started_at: float | None
    ) -> ActivityState:
        """Derive this agent's activity state from the cached signals.

        The two universal gates run first, for every harness: a positively-dead
        lifecycle (STOPPED and friends -- never UNKNOWN, which is non-evidence)
        settles to IDLE (the process's in-memory queue and in-flight turn died
        with it), and a transcript tail predating the current process's marker
        (``process_started_at``, the mtime of :attr:`marker_filename`, or None
        when absent) is a turn a dead process abandoned. Only then does the
        harness's own :meth:`_derive_working` decide what a live turn reads as.
        """
        if is_lifecycle_dead(lifecycle_state):
            return ActivityState.IDLE
        if is_transcript_tail_stale(tail_event_at=self._tail_event_at, process_started_at=process_started_at):
            return ActivityState.IDLE
        return self._derive_working(
            lifecycle_state=lifecycle_state,
            is_active_marker_present=is_active_marker_present,
            process_started_at=process_started_at,
        )

    @abstractmethod
    def _derive_working(
        self, *, lifecycle_state: str, is_active_marker_present: bool, process_started_at: float | None
    ) -> ActivityState:
        """Is a turn in flight, and is it a tool or thinking? The process is not
        positively dead and the transcript is known current. A harness is free to
        ignore inputs it declares away (codex reads neither the lifecycle nor the
        marker -- consistent with ``active_marker_filename = None``)."""

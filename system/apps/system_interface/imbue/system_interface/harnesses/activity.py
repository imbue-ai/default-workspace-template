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
from imbue.system_interface.activity_state import is_lifecycle_dead
from imbue.system_interface.activity_state import is_non_turn_tail_event
from imbue.system_interface.activity_state import is_transcript_tail_stale
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
    # Incremental fold state: the id-keyed tool pairing (order-independent, so
    # re-delivered events cannot corrupt it) and the timestamp-guarded tail markers.
    _pending_tool_call_ids: set[str]
    _matched_tool_call_ids: set[str]
    _folded_last_type: str | None
    _folded_last_timestamp: str | None
    _last_turn_type_at: float | None
    _last_event_at: float | None

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
        self._pending_tool_call_ids = set()
        self._matched_tool_call_ids = set()
        self._folded_last_type = None
        self._folded_last_timestamp = None
        self._last_turn_type_at = None
        self._last_event_at = None
        self._reset_extra()

    @property
    def _tail_event_at(self) -> float | None:
        """The cached tail event's timestamp as epoch seconds, or None."""
        return parse_iso_timestamp_to_epoch(self._last_event_timestamp)

    def observe(self, events: Sequence[dict[str, Any]]) -> bool:
        """Fold a batch of events into the cached signals.

        Incremental on purpose: the watcher hands over exactly the events it just parsed
        (or the whole backlog once, at seeding), never the full transcript per event -- the
        old full-list rescan was O(N) per event and re-materialised the transcript each
        time. A batch can also carry RE-BROADCAST events (a supersession or a late subagent
        enrichment of an OLD event), so every tail signal advances only for events whose
        timestamp is at or past the signal's current position -- an old event re-delivered
        cannot drag the tail backwards. The pending/matched tool-call sets are id-keyed and
        order-independent, so re-delivery cannot corrupt them either.

        Returns True iff a signal that affects derivation changed -- callers use that to
        skip an otherwise-pointless recompute (and its marker ``stat``) for streamed events
        that move nothing.
        """
        for event in events:
            event_at = parse_iso_timestamp_to_epoch(event.get("timestamp"))
            self._fold_common(event, event_at)
            self._fold_extra_event(event, event_at)
        new_pending = bool(self._pending_tool_call_ids - self._matched_tool_call_ids)
        new_last_type = self._folded_last_type
        new_extra = self._current_extra()
        if (
            new_pending == self._has_pending_tool_use
            and new_last_type == self._last_event_type
            and new_extra == self._extra
        ):
            return False
        self._has_pending_tool_use = new_pending
        self._last_event_type = new_last_type
        self._extra = new_extra
        self._last_event_timestamp = self._folded_last_timestamp
        return True

    def _fold_common(self, event: dict[str, Any], event_at: float | None) -> None:
        """Fold one event into the shared signals (tool pairing, tail type/timestamp)."""
        event_type = event.get("type")
        if event_type == "assistant_message":
            for tool_call in event.get("tool_calls") or ():
                tool_call_id = tool_call.get("tool_call_id")
                if tool_call_id:
                    self._pending_tool_call_ids.add(tool_call_id)
        elif event_type == "tool_result":
            tool_call_id = event.get("tool_call_id")
            if tool_call_id:
                self._matched_tool_call_ids.add(tool_call_id)
        else:
            # user_message and markers carry no tool pairing.
            pass
        # The tail type skips non-turn events (a bar-sent /model and its confirmation must
        # not pin "Thinking..."); the tail timestamp tracks every event. Both advance only
        # forward in time so a re-broadcast old event cannot regress them; an unparseable
        # timestamp advances.
        if self._advances(event_at, self._last_turn_type_at) and not is_non_turn_tail_event(event):
            self._folded_last_type = event_type if isinstance(event_type, str) else None
            self._last_turn_type_at = event_at if event_at is not None else self._last_turn_type_at
        if self._advances(event_at, self._last_event_at):
            timestamp = event.get("timestamp")
            self._folded_last_timestamp = timestamp if isinstance(timestamp, str) and timestamp else None
            self._last_event_at = event_at if event_at is not None else self._last_event_at

    @staticmethod
    def _advances(event_at: float | None, current_at: float | None) -> bool:
        """Whether an event at ``event_at`` may advance a tail signal at ``current_at``."""
        return event_at is None or current_at is None or event_at >= current_at

    def _fold_extra_event(self, event: dict[str, Any], event_at: float | None) -> None:
        """Fold one event into this harness's extra signals. No-op by default."""

    def _current_extra(self) -> tuple[Any, ...]:
        """This harness's extra cached signals, as folded so far. Empty by default."""
        return ()

    def _reset_extra(self) -> None:
        """Clear this harness's extra folded state. No-op by default."""

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

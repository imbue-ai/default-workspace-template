"""opencode's activity tracker.

Unlike claude/pi -- whose transcripts carry no "busy" signal, so activity is inferred from
the transcript tail -- opencode has a REAL busy signal: the mngr lifecycle ``active`` marker,
which the plugin sets the instant a turn goes ``session.status: busy`` and clears on the root
session's idle (verified live: the marker flips within ~30ms of the send). So opencode drives
activity off that marker (a codex-style latch), NOT the claude tail heuristic.

This matters for parity: opencode PRE-CREATES the assistant ``message`` row at turn start (the
empty streaming placeholder appears alongside the user message), so the transcript tail is
``assistant_message`` almost immediately. The claude tail heuristic reads that as IDLE during
generation -- so a plain Q&A turn would never show "Thinking". Gating on the ``active`` marker
instead makes "Thinking" appear the moment processing begins, exactly as it does for pi.

Derivation:
  * not running (marker absent / lifecycle STOPPED) -> IDLE;
  * running with an unmatched ``tool_use`` -> TOOL_RUNNING;
  * running otherwise -> THINKING (the model is working, tool or not).

``is_agent_running`` is resolved upstream (``resolve_is_agent_running``) preferring the
``active`` marker over the laggy observe-reported lifecycle, and reads False for a STOPPED/
EXITED agent -- so a stale marker left by a crash never pins THINKING (the plugin also clears
the marker at startup and on idle).
"""

from collections.abc import Sequence
from typing import Any
from typing import ClassVar

from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.activity_state import has_unmatched_tool_use
from imbue.system_interface.activity_state import last_event_timestamp
from imbue.system_interface.activity_state import last_event_type
from imbue.system_interface.harnesses.activity import HarnessActivityTracker


class OpenCodeActivityTracker(HarnessActivityTracker):
    """opencode: activity is the ``active`` lifecycle marker plus an unmatched-tool check."""

    # opencode's launch script clears then writes this readiness sentinel on every
    # startup/resume once the server is up; its mtime bounds transcript staleness for the
    # shared machinery. Kept in sync with READY_SENTINEL_FILENAME in mngr_opencode's config.
    marker_filename: ClassVar[str] = "opencode_ready"

    def observe(self, events: Sequence[dict[str, Any]]) -> bool:
        new_pending = has_unmatched_tool_use(events)
        new_last_type = last_event_type(events)
        if new_pending == self._has_pending_tool_use and new_last_type == self._last_event_type:
            return False
        self._has_pending_tool_use = new_pending
        self._last_event_type = new_last_type
        self._last_event_timestamp = last_event_timestamp(events)
        return True

    def derive(self, *, is_agent_running: bool, process_started_at: float | None) -> ActivityState:
        # Marker-driven latch (see the module docstring). ``process_started_at`` is unused: with
        # a real busy marker there is no stale-tail to guard against -- the marker itself is the
        # authority, and ``is_agent_running`` already reads False once the process is not RUNNING.
        if not is_agent_running:
            return ActivityState.IDLE
        if self._has_pending_tool_use:
            return ActivityState.TOOL_RUNNING
        return ActivityState.THINKING

"""Grok's activity-state derivation.

Whether a turn is in flight is mngr's answer; the transcript says whether a top-level tool call is still running. A
quit answers no tool call, so a call made before the current grok process started does not count.
"""

from collections.abc import Collection

from imbue.chat.activity_state import ActivityState
from imbue.chat.activity_state import is_transcript_tail_stale
from imbue.imbue_common.pure import pure


@pure
def derive(
    *,
    is_turn_in_flight: bool,
    # When each top-level tool call still without a result was made, or None when its event carries no time.
    pending_tool_call_ats: Collection[float | None],
    tail_event_at: float | None = None,
    process_started_at: float | None = None,
) -> ActivityState:
    """Derive an ``ActivityState`` for a grok agent from mngr's turn-in-flight answer and the unanswered tool calls."""
    if not is_turn_in_flight:
        return ActivityState.IDLE
    if is_transcript_tail_stale(tail_event_at=tail_event_at, process_started_at=process_started_at):
        return ActivityState.IDLE
    is_tool_running = any(
        not is_transcript_tail_stale(tail_event_at=called_at, process_started_at=process_started_at)
        for called_at in pending_tool_call_ats
    )
    return ActivityState.TOOL_RUNNING if is_tool_running else ActivityState.THINKING

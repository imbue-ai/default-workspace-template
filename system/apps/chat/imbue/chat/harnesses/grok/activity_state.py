"""Grok's activity-state derivation, from the turn markers of its top-level sessions.

Grok writes a ``turn_completed`` record for every turn it ends, normal, cancelled or failed
(surfaced by :mod:`harnesses.grok.session_parser` as ``turn_started`` / ``turn_completed`` /
``turn_aborted`` special events of a top-level session), so "the agent is working" is a latch on
those. It is kept per top-level session, because ``/new`` opens a second one in the same process.
A quit writes no ``turn_completed`` and answers no tool call, so an open turn or an unanswered call
from before the current grok process started belongs to a dead process and does not count.
"""

from collections.abc import Collection

from imbue.chat.activity_state import ActivityState
from imbue.chat.activity_state import is_transcript_tail_stale
from imbue.imbue_common.pure import pure


@pure
def _is_in_current_process(event_at: float | None, process_started_at: float | None) -> bool:
    return not is_transcript_tail_stale(tail_event_at=event_at, process_started_at=process_started_at)


@pure
def derive(
    *,
    # When each top-level session's open turn began, or None when its marker carries no time.
    open_turn_started_ats: Collection[float | None],
    # When each tool call still without a result was made, or None when its event carries no time.
    pending_tool_call_ats: Collection[float | None],
    tail_event_at: float | None = None,
    process_started_at: float | None = None,
) -> ActivityState:
    """Derive an ``ActivityState`` for a grok agent from its open top-level turns and unanswered tool calls.

    Priority:
      1. transcript tail predates the current process (stale) -> IDLE.
      2. no open turn that the current process began -> IDLE.
      3. a tool call the current process made is unanswered -> TOOL_RUNNING.
      4. otherwise -> THINKING.
    """
    if is_transcript_tail_stale(tail_event_at=tail_event_at, process_started_at=process_started_at):
        return ActivityState.IDLE
    if not any(_is_in_current_process(started_at, process_started_at) for started_at in open_turn_started_ats):
        return ActivityState.IDLE
    if any(_is_in_current_process(called_at, process_started_at) for called_at in pending_tool_call_ats):
        return ActivityState.TOOL_RUNNING
    return ActivityState.THINKING

"""antigravity's activity-state derivation.

The antigravity peer of :mod:`claude_activity_state` / :mod:`codex_activity_state`. Like
claude, agy has no turn-boundary events, so "the agent is working" is inferred from the
mngr lifecycle (RUNNING) plus the transcript tail. Two things differ from claude:

1. **The stale-tail rung lives in the shared base, not here.** Like claude and codex, agy
   guards a mid-turn restart by comparing the tail against its ``*_process_started`` marker
   (mngr_antigravity stamps ``antigravity_process_started`` on every launch/resume) -- the
   base applies that gate before this function is reached. It matters for agy specifically
   because agy RESUMES its own store, so a restarted agent's transcript still carries the
   dead process's unmatched tool call. What this function need not re-check is liveness: the
   mngr ``active`` marker (surfaced as ``is_agent_running``) is removed when agy goes idle.

2. **An empty planner step is not the end of the turn.** agy emits a PLANNER_RESPONSE step
   carrying only ``thinking`` (empty text) before each tool call -- its "deciding what to
   do next" step. Treating that empty assistant tail as a finished answer (as claude's
   derive would) flickers the indicator to IDLE between every tool. So a tail
   ``assistant_message`` means the turn is over only when it carries real answer text;
   an empty one, mid-turn, stays THINKING.
"""

from imbue.imbue_common.pure import pure
from imbue.system_interface.activity_state import ActivityState


@pure
def derive(
    *,
    is_agent_running: bool,
    has_pending_tool_use: bool,
    tail_event_type: str | None,
    tail_is_final_answer: bool,
) -> ActivityState:
    """Derive an ``ActivityState`` for an agy agent from lifecycle + transcript signals.

    ``is_agent_running`` reflects the mngr lifecycle (the ``active`` marker). ``tail_is_final_answer``
    is True only when the tail event is an ``assistant_message`` carrying real answer text.

    Priority:
      0. agent not running -> IDLE (also closes off a lingering async run_command and an
         interrupted turn -- no synthetic marker needed).
      1. a tool call in flight (dispatched, result not yet in) -> TOOL_RUNNING.
      2. tail is ``user_message`` / ``tool_result`` -> THINKING.
      3. tail is an empty planner step (thinking, no text) mid-turn -> THINKING.
      4. tail is a substantive assistant answer -> IDLE.
      5. otherwise -> IDLE.
    """
    if not is_agent_running:
        return ActivityState.IDLE
    if has_pending_tool_use:
        return ActivityState.TOOL_RUNNING
    if tail_event_type in ("user_message", "tool_result"):
        return ActivityState.THINKING
    if tail_event_type == "assistant_message":
        return ActivityState.IDLE if tail_is_final_answer else ActivityState.THINKING
    return ActivityState.IDLE

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
from imbue.system_interface.activity_state import is_transcript_tail_stale


@pure
def derive(
    *,
    is_agent_alive: bool,
    has_pending_tool_use: bool,
    tail_event_type: str | None,
    tail_is_final_answer: bool,
    tail_event_at: float | None = None,
    process_started_at: float | None = None,
) -> ActivityState:
    """Derive an ``ActivityState`` for an agy agent from lifecycle + transcript signals.

    Deliberately claude's ladder, rung for rung, with ONE rung inserted -- see 3a. Keeping the
    shapes identical is the point: agy has no turn markers either, so any divergence beyond the
    one agy genuinely needs would be an accident rather than a decision.

    The marker is a SUPPORTING signal here, never an override -- this is the one place agy
    must diverge from claude's rung 0, and it is why. agy's statusLine reports only two
    states, ``idle`` and ``thinking``; there is no ``tool_calling``. So the ``active`` marker
    is removed for the whole of every tool call, and a marker-gated rung 0 would return IDLE
    mid-tool-chain -- skipping the very rungs (2, 3) that know a tool is running. Downstream
    that IDLE arms the queue flush, which types into a live turn and gets the block merged
    into it: an A1a swallow, observed in production. Liveness (is the PROCESS gone) still
    short-circuits; "is agy thinking right now" does not.

    ``tail_event_at`` / ``process_started_at`` feed
    :func:`activity_state.is_transcript_tail_stale` to drop a turn abandoned by a prior process
    (a mid-turn restart) -- which matters MORE for agy than for claude, because agy resumes its
    own conversation store, so the dead process's unmatched tool call is still present.

    Priority:
      0. agent process is dead -> IDLE. (Liveness only. NOT the ``active`` marker -- see above.)
      1. transcript tail predates the current process (stale) -> IDLE.
      2. unmatched tool call -> TOOL_RUNNING.
      3. last transcript event is ``user_message`` or ``tool_result`` -> THINKING.
      3a. AGY ONLY: last event is an ``assistant_message`` with no text -> THINKING. agy writes
          an empty PLANNER_RESPONSE ("deciding what to do next") before each tool call; claude's
          rung 4 would read that as a finished answer and flicker the dot to IDLE between every
          single tool.
      4. otherwise (a substantive ``assistant_message``) -> IDLE.
    """
    if not is_agent_alive:
        return ActivityState.IDLE
    if is_transcript_tail_stale(tail_event_at=tail_event_at, process_started_at=process_started_at):
        return ActivityState.IDLE
    if has_pending_tool_use:
        return ActivityState.TOOL_RUNNING
    if tail_event_type in ("user_message", "tool_result"):
        return ActivityState.THINKING
    if tail_event_type == "assistant_message" and not tail_is_final_answer:
        return ActivityState.THINKING
    return ActivityState.IDLE

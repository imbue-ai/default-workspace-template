"""Codex's activity-state derivation -- the codex peer of :mod:`harnesses.claude.activity_state`.

Unlike claude, codex writes authoritative turn-boundary markers to its rollout in real time --
``task_started`` when a user turn begins, ``task_complete`` / ``turn_aborted`` when it ends (surfaced
by :mod:`harnesses.codex.session_parser` as ``turn_started`` / ``turn_completed`` / ``turn_aborted``
special events). So "the agent is working" is a simple latch on those, with **no reliance on the
(unreliable-for-codex, and polled-hence-laggy) mngr lifecycle**. Verified against real rollouts:
``task_started`` lands ~seconds before the first assistant text and ``task_complete`` just after the
last, so the dot stays lit across the whole turn and clears only once the text is on screen.

Within an open turn the transcript refines *what* the agent is doing: an in-flight tool call reads
TOOL_RUNNING, which the client renders as the tool's own verb ("Running", "Web search", ...) from the
labels each ``tool_call`` event already carries; plain reasoning reads THINKING.
"""

from imbue.imbue_common.pure import pure
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.activity_state import is_transcript_tail_stale


@pure
def derive(
    *,
    turn_open: bool,
    has_pending_tool_use: bool,
    tail_event_at: float | None = None,
    process_started_at: float | None = None,
) -> ActivityState:
    """Derive an ``ActivityState`` for a codex agent from the transcript turn latch.

    ``turn_open`` is the tracker's folded turn latch (the latest turn marker opened a turn). ``tail_event_at`` / ``process_started_at`` feed
    :func:`activity_state.is_transcript_tail_stale` (using the ``codex_process_started`` marker) so a
    turn abandoned by a prior process (a mid-turn restart that left an unclosed ``task_started``) reads
    IDLE rather than pinned "Thinking...".

    Priority:
      1. transcript tail predates the current process (stale) -> IDLE (restart guard).
      2. no open turn -> IDLE (the authoritative waiting-for-the-user signal).
      3. a tool call in flight -> TOOL_RUNNING.
      4. otherwise (turn open, no tool) -> THINKING.
    """
    if is_transcript_tail_stale(tail_event_at=tail_event_at, process_started_at=process_started_at):
        return ActivityState.IDLE
    if not turn_open:
        return ActivityState.IDLE
    if has_pending_tool_use:
        return ActivityState.TOOL_RUNNING
    return ActivityState.THINKING

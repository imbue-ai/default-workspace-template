"""Shared primitives for per-agent activity state surfaced on the chat panel.

Holds the common building blocks the harness trackers use: the ``ActivityState``
enum, the ``is_non_turn_tail_event`` reader (the fold's tail-type gate), the
timestamp parser, and the ``is_transcript_tail_stale`` restart guard. The actual IDLE / THINKING / TOOL_RUNNING *derivation* lives in the
two harness peers -- :mod:`claude_activity_state` (lifecycle + transcript tail) and
:mod:`codex_activity_state` (the ``task_started`` / ``task_complete`` turn latch) --
and the harness dispatch is in ``agent_manager._recompute_activity_state``.

The ``*_process_started`` marker (touched by mngr on every startup/resume) is the
boundary the stale-tail guard compares against: a transcript tail older than the
current process is left over from a turn this process never ran and must not show
"Thinking..." indefinitely after a mid-turn restart.
"""

from collections.abc import Sequence
from datetime import datetime
from enum import auto
from typing import Any

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.pure import pure


class ActivityState(UpperCaseStrEnum):
    """The activity state of a chat agent, as surfaced above the message input."""

    IDLE = auto()
    THINKING = auto()
    TOOL_RUNNING = auto()


@pure
def is_non_turn_tail_event(event: dict[str, Any]) -> bool:
    """True for a trailing transcript event that is NOT a genuine turn awaiting a reply.

    The PARSER decides (``harnesses/message_display.is_non_turn_tail``, stamped as the
    event's ``non_turn_tail`` field); this just reads the decision. One implementation --
    the detector table that also drives rendering -- instead of the hand-mirrored copy of
    the frontend's regexes that used to live here.
    """
    return bool(event.get("non_turn_tail"))


@pure
def parse_iso_timestamp_to_epoch(timestamp: str | None) -> float | None:
    """Parse an ISO-8601 transcript timestamp (e.g. ``2026-06-08T19:42:15.191Z``)
    into epoch seconds, or ``None`` if it is missing or unparseable.

    Claude writes UTC timestamps with a trailing ``Z``; ``fromisoformat`` accepts
    that on the Python versions this app targets. The result is an absolute epoch,
    directly comparable to a filesystem mtime regardless of timezone.
    """
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp).timestamp()
    except ValueError:
        return None


RUNNING_LIFECYCLE_STATES: frozenset[str] = frozenset({"RUNNING", "RUNNING_UNKNOWN_AGENT_TYPE"})

# mngr's lifecycle reports RUNNING iff this marker file exists in the agent state dir while
# the process is alive; every harness's plugin writes the same filename. Reading it directly
# is a *timely* alternative to the observe-reported lifecycle state (see
# :func:`resolve_is_agent_running`).
ACTIVE_MARKER_FILENAME: str = "active"

# The lifecycle state of an alive-but-idle agent (between turns). This is the ONLY state where
# the observe-reported state can trail the real turn: a quick turn sets and clears the `active`
# marker before the observe stream reports RUNNING, leaving the reported state at WAITING the
# whole time.
WAITING_LIFECYCLE_STATE: str = "WAITING"

# The lifecycle verdict meaning "could not observe", not "dead": mngr maps provider and probe
# failures here (see mngr's ``AgentLifecycleState.UNKNOWN``). Consumers treat it as
# non-evidence, so live state (the activity dot, the queued-message mirror) must never be
# wiped on its account.
UNKNOWN_LIFECYCLE_STATE: str = "UNKNOWN"


@pure
def is_lifecycle_dead(lifecycle_state: str) -> bool:
    """True iff the observe-reported lifecycle positively says the agent process is dead.

    Dead is everything outside the RUNNING states, WAITING (alive between turns), and
    UNKNOWN (unobservable -- non-evidence, never treated as death). A dead process's
    in-memory queue and in-flight turn died with it, so its activity must settle to IDLE
    no matter what the transcript tail says; the manager applies that override in
    ``_recompute_activity_state``.
    """
    if lifecycle_state in RUNNING_LIFECYCLE_STATES:
        return False
    if lifecycle_state == WAITING_LIFECYCLE_STATE:
        return False
    return lifecycle_state != UNKNOWN_LIFECYCLE_STATE


@pure
def resolve_is_agent_running(lifecycle_state: str, is_active_marker_present: bool) -> bool:
    """Whether the agent has a turn in flight, preferring the ``active`` marker over the
    (laggy) observe-reported lifecycle state.

    RUNNING states are authoritative. In the alive-but-idle WAITING state the marker breaks the
    tie: the observe stream can miss a short turn, so the reported state stays WAITING while the
    marker itself flips promptly -- trust the marker there. Any other state (STOPPED / EXITED /
    ...) reads as not running, so a hard-crashed agent's stale marker is never mistaken for a
    live turn. Each harness's plugin also clears the marker at launch, so a
    marker outliving its process cannot be mistaken for a live turn by anything that reads it
    directly either.
    """
    if lifecycle_state in RUNNING_LIFECYCLE_STATES:
        return True
    if lifecycle_state == WAITING_LIFECYCLE_STATE:
        return is_active_marker_present
    return False


@pure
def is_transcript_tail_stale(
    *,
    tail_event_at: float | None,
    process_started_at: float | None,
) -> bool:
    """True iff the newest transcript event predates the current Claude process.

    ``tail_event_at`` is the epoch time of the final transcript event;
    ``process_started_at`` is the mtime of the agent's ``claude_process_started``
    marker, which mngr touches on every startup/resume (a fresh, not-mid-turn
    process). When the newest event is older than that boundary, it belongs to a
    turn the *current* process never ran -- e.g. a turn abandoned mid-flight when
    a container restart killed Claude. Its "still working" tail (an unmatched
    ``tool_use`` or a trailing ``tool_result``) would otherwise pin the indicator
    at TOOL_RUNNING / THINKING forever, since the dead turn will never emit the
    closing ``assistant_message`` that settles it back to IDLE.

    Returns ``False`` when either input is missing (no marker yet, or a final
    event without a timestamp): we only override on positive evidence of
    staleness, otherwise the transcript signals stand.
    """
    if tail_event_at is None or process_started_at is None:
        return False
    return tail_event_at < process_started_at

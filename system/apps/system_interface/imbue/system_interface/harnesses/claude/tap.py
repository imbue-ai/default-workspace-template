"""Claude's native shoulder tap: flush the parked message queue into the live turn.

The shoulder-tap contract (system/apps/system_interface/imbue/system_interface/harnesses/core-contracts/messages-lifecycle-contract.md, Part B) for
claude, WITHOUT the SIGKILL-restart the base ``/flush-queue`` path uses. Cancelling
claude's live turn makes it flush its parked queue through immediately -- the same auto-flush it performs at natural turn end. We
trigger that flush early by delivering a Chat-only ``meta+q`` -> ``chat:cancel`` chord
(mngr provisions the binding; ``meta+q`` is inert in every non-Chat context, so a stray
delivery can never be reinterpreted as ``confirm:no`` / ``autocomplete:dismiss`` / ...).

The keypress itself is NOT done here: dwt never drives raw tmux. It routes through mngr's
in-process message API (``send_key_chord_to_agents`` -> ``press_key_chord``), which holds
the same per-agent ``message.lock`` as a text send, so the chord can never interleave with
a half-delivered message. This module owns only the orchestration around that keypress:
the refresh-first gate, the live-session byte baseline, the bounded verdict watch, and the
one recovery message for the cancelled-follow-on race. The abort-and-capture pieces
(``resolve_live_session_baseline`` / ``watch_for_flush_verdict``) and the pure verdict
lattice are factored so the sibling plan-claude-interrupt path can reuse them.

The watch is biased toward *never losing* a message: it fast-exits FLUSHED once the mirror has
drained AND the flush is confirmed to have started a turn -- either the turn produced an answer OR
a turn was seen alive at some poll since the chord (it started; a quick turn may start and finish
inside the window). A drained mirror with a dangling interrupt sentinel where a turn was NEVER seen
alive is NEEDS_RECOVERY (the flush committed the messages but nothing ran to answer them -- the
cancelled follow-on). A mirror that never drains is NOT_FLUSHED -- so the accepted failure modes
are a spurious recovery message or a spurious 500 (both visible), never a silently stranded queue.

This module ALSO owns claude's stop button (Contract B) for the EMPTY-queue case
(:class:`ClaudeInterruptToComposer` / :func:`execute_claude_stop_to_composer`): the same
``meta+q`` chord, but resolved as a pure interrupt -- confirm the abort by the interrupt
sentinel appearing past the baseline (either shape), then mark the agent idle -- rather than
a flush. A NONEMPTY queue (or a dialog, inactive binding, or unconfirmed abort) delegates to
the base restart-drain, with the mirror refreshed and the block captured under mngr's bounded
per-agent ``message.lock`` -- so a message an in-flight send parks mid-stop rides the returned
block instead of dying silently with the SIGKILL; when the bounded wait expires, stop still
wins on a fresh best-effort re-capture. The two paths share the same module so the stop's cancel chord and
the tap's recovery arm coordinate through one in-process stop-timestamp registry: a stop
pressed inside the tap's watch suppresses the tap's recovery resend (it would otherwise
re-drive the just-stopped turn).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from enum import StrEnum
from pathlib import Path
from typing import Any
from typing import Protocol

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_claude.claude_config import ensure_chat_cancel_tap_keybinding
from imbue.mngr_claude.claude_config import is_tap_binding_active
from imbue.mngr_claude.claude_config import mark_claude_agent_idle
from imbue.system_interface.activity_state import ACTIVE_MARKER_FILENAME
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_discovery import get_host_dir
from imbue.system_interface.harnesses.claude.session_parser import INTERRUPT_SENTINEL_TEXT
from imbue.system_interface.harnesses.claude.session_parser import extract_text_content
from imbue.system_interface.harnesses.claude.session_parser import is_interrupt_sentinel_text
from imbue.system_interface.harnesses.interrupt import InterruptToComposer
from imbue.system_interface.harnesses.interrupt import PressChord
from imbue.system_interface.harnesses.interrupt import RestartProcess
from imbue.system_interface.harnesses.interrupt import SettleActivity
from imbue.system_interface.harnesses.interrupt import restart_drain
from imbue.system_interface.harnesses.interrupt import try_hold_message_lock
from imbue.system_interface.harnesses.session import AtomicShoulderTap
from imbue.system_interface.harnesses.session import ShoulderTapOutcome
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher

# The tmux key token the chord is delivered as (Meta+q == ESC then q, one pty write).
TAP_CHORD: str = "M-q"

# The user-scope keybindings file (beside the config dir) that mngr provisions the chord
# into and the gate reads. Kept in sync with mngr's KEYBINDINGS_FILENAME.
KEYBINDINGS_FILENAME: str = "keybindings.json"

# mngr's per-agent state markers the gates read. ``active`` is present while a turn is in
# flight; ``permissions_waiting`` is present while claude blocks on a dialog (a context the
# Chat-only chord is inert in, so we fail fast rather than hang the whole watch); the
# process-start marker's mtime bounds ``is_tap_binding_active``.
PERMISSIONS_WAITING_MARKER_FILENAME: str = "permissions_waiting"
CLAUDE_PROCESS_STARTED_MARKER_FILENAME: str = "claude_process_started"

# The recovery message sent when the chord cancelled the flushed follow-on turn (the
# messages were committed but their turn never answered). It rides the existing injected
# task-notification family: chip-rendered, and phantom in the queue tracker if it ever parks.
RECOVERY_MESSAGE: str = (
    "<task-notification>Queued messages above were delivered but their turn was interrupted; "
    "please address them now.</task-notification>"
)

# Raw session-record type tags.
_USER_RECORD_TYPE: str = "user"
_ASSISTANT_RECORD_TYPE: str = "assistant"

# Watch tuning: poll ~200ms up to 3s. The chord's effect (flush + answer, or a cancelled
# follow-on) lands well inside this on the gate-observed trace.
_WATCH_DEADLINE_SECONDS: float = 3.0
_POLL_INTERVAL_SECONDS: float = 0.2

# The stop button's abort watch is held longer than the tap's flush watch: 8s covers
# wait_for_stop_hook.sh's floor (GRACE_PERIOD=3 + transcript flush) for the marker-vanish arm
# when no other Stop hooks run. With other Stop hooks provisioned the hook-wait can exceed any
# deadline, so the marker-vanish arm is best-effort; the interrupt sentinel confirms the abort
# directly and does not depend on it.
_INTERRUPT_WATCH_DEADLINE_SECONDS: float = 8.0

# In-process per-agent stop timestamps (shared monotonic clock), keyed by agent state-dir
# string. The stop executor records here when it delivers its cancel chord; the tap's
# NEEDS_RECOVERY arm suppresses its resend when a stop ran since the tap's baseline. Without
# this, a stop pressed inside the tap's watch matches the recovery signature exactly (drained
# mirror + a post-baseline sentinel) and the recovery message re-drives the just-stopped turn.
_STOP_MONOTONIC_BY_AGENT: dict[str, float] = {}


def _record_stop(agent_key: str, *, now: Callable[[], float]) -> None:
    """Record that a stop-button interrupt fired for ``agent_key`` at the shared monotonic clock."""
    _STOP_MONOTONIC_BY_AGENT[agent_key] = now()


def _stop_ran_since(agent_key: str, since: float) -> bool:
    """True iff a stop was recorded for ``agent_key`` at or after ``since`` (a tap's baseline time)."""
    recorded = _STOP_MONOTONIC_BY_AGENT.get(agent_key)
    return recorded is not None and recorded >= since


class TapWatcher(Protocol):
    """The slice of the session watcher the tap needs. A Protocol so tests inject a fake.

    The base ``AgentSessionWatcher`` structurally satisfies it (the three methods live on the
    shared shoulder-tap surface, with the claude watcher overriding ``get_latest_main_session_file``),
    so the server passes its base-typed watcher straight through without a narrowing cast.
    """

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Read session files and return parsed events; the single point that refreshes the mirror."""
        ...

    def get_queued_messages(self) -> list[dict[str, Any]]:
        """The current parked-queue mirror snapshot (empty == drained)."""
        ...

    def get_latest_main_session_file(self) -> Path | None:
        """The JSONL file of the live process's (latest main) session, or None."""
        ...


class TapVerdict(StrEnum):
    """The terminal reading of the post-chord watch.

    - FLUSHED: mirror drained and the flush started a turn -- EITHER it produced an answer, OR a turn
      was seen alive at some poll in the window (it started; it may have already finished).
    - NEEDS_RECOVERY: mirror drained, a post-baseline interrupt sentinel has no answer after it, AND a
      turn was never seen alive in the window -- the flush committed the messages but nothing ran to
      answer them (the cancelled follow-on); the committed messages need a nudge.
    - NOT_FLUSHED: the deadline passed with the mirror still non-empty; the flush did not go through.
    """

    FLUSHED = "flushed"
    NEEDS_RECOVERY = "needs_recovery"
    NOT_FLUSHED = "not_flushed"


class ClaudeTapStatus(StrEnum):
    """The executor's outcome; the server maps it to an HTTP response.

    The 200 no-ops are NOTHING_QUEUED (mirror already empty after refresh), NO_OPEN_TURN
    (no turn in flight, so no chord delivered), and SEND_IN_FLIGHT (a message send held
    ``message.lock`` past the bounded wait, so nothing was flushed -- a benign no-op, not a
    user-facing error: the button is greyed by the backend availability flag whenever a send
    is in flight, so a tap that still races one just does nothing and the user can retap);
    TAPPED is the 200 success (flushed, and on the race its recovery message sent). The rest
    are errors the server surfaces: PERMISSIONS_WAITING (409, claude is on a dialog the chord
    is inert in), BINDING_NOT_ACTIVE (500, the chord is not live for this process yet),
    CHORD_SEND_FAILED / RECOVERY_SEND_FAILED (500, a delivery failed), and NOT_FLUSHED (500,
    the queue did not flush within the watch window).
    """

    NOTHING_QUEUED = "nothing_queued"
    NO_OPEN_TURN = "no_open_turn"
    PERMISSIONS_WAITING = "permissions_waiting"
    BINDING_NOT_ACTIVE = "binding_not_active"
    CHORD_SEND_FAILED = "chord_send_failed"
    NOT_FLUSHED = "not_flushed"
    RECOVERY_SEND_FAILED = "recovery_send_failed"
    SEND_IN_FLIGHT = "send_in_flight"
    TAPPED = "tapped"


# The statuses that map to a 200 (a successful tap or an idempotent no-op). SEND_IN_FLIGHT is a
# benign no-op, not an error: the backend availability flag greys the button while a send is in
# flight, so a tap that still races one flushes nothing and the user simply retaps -- surfacing a
# 500 there is the button-then-error bug we are removing.
OK_TAP_STATUSES: frozenset[ClaudeTapStatus] = frozenset(
    {
        ClaudeTapStatus.NOTHING_QUEUED,
        ClaudeTapStatus.NO_OPEN_TURN,
        ClaudeTapStatus.SEND_IN_FLIGHT,
        ClaudeTapStatus.TAPPED,
    }
)


class ClaudeTapResult(FrozenModel):
    """The executor's return: a status plus, for error statuses, a user-facing detail."""

    status: ClaudeTapStatus = Field(description="The tap outcome")
    detail: str = Field(default="", description="A user-facing message for the error statuses")


class _TailFacts(FrozenModel):
    """What the raw post-baseline tail tells us about the tap's effect."""

    has_interrupt_sentinel: bool = Field(description="A post-baseline interrupt sentinel is present")
    has_assistant_answer: bool = Field(
        description="An assistant record appears after the last sentinel (or after the baseline when none)"
    )


def _load_json_object(line: str) -> dict[str, Any] | None:
    """Parse one raw session line as a JSON object, or None if it is not one.

    A malformed complete line (partial trailing lines are already dropped upstream) is
    surfaced at warning level rather than swallowed -- it means on-disk corruption worth
    noticing, matching the parser's own queue-signal handling.
    """
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as e:
        logger.warning("Skipping non-JSON session line while classifying the tap tail: {}", e)
        return None
    return raw if isinstance(raw, dict) else None


def _is_interrupt_sentinel_record(raw: dict[str, Any]) -> bool:
    """True iff ``raw`` is the user record claude writes when a turn is interrupted.

    Matches the plain streaming-abort sentinel (``[Request interrupted by user]``). The
    mid-tool ``for tool use`` variant is a different string and is deliberately NOT matched
    here (it is the sibling interrupt plan's concern). Mirrors the parser's own suppression
    (``_parse_user_message``), which is why the raw tail -- not parsed events -- is scanned.
    """
    if raw.get("type") != _USER_RECORD_TYPE:
        return False
    message = raw.get("message")
    if not isinstance(message, dict):
        return False
    return extract_text_content(message.get("content")).strip() == INTERRUPT_SENTINEL_TEXT


def read_raw_tail(session_file: Path, baseline_size: int) -> list[str]:
    """Return the complete (newline-terminated) raw lines appended past ``baseline_size``.

    Only whole records are returned; a trailing partial line (claude mid-append) is dropped
    so we never parse half a record. Empty when the file has not grown, shrank below the
    baseline (a rewrite), or cannot be read.
    """
    try:
        data = session_file.read_bytes()
    except OSError:
        return []
    if baseline_size < 0 or baseline_size >= len(data):
        return []
    tail = data[baseline_size:]
    last_newline = tail.rfind(b"\n")
    if last_newline == -1:
        return []
    complete = tail[: last_newline + 1].decode("utf-8", errors="replace")
    return [line for line in complete.splitlines() if line.strip()]


def compute_tail_facts(tail_lines: list[str]) -> _TailFacts:
    """Reduce the raw post-baseline tail to the two facts the verdict lattice keys on."""
    last_sentinel_index = -1
    last_assistant_index = -1
    for index, line in enumerate(tail_lines):
        raw = _load_json_object(line)
        if raw is None:
            continue
        # A record is at most one of these (a sentinel is a ``user`` record), so two
        # independent checks are equivalent to -- and clearer than -- an if/elif chain.
        if _is_interrupt_sentinel_record(raw):
            last_sentinel_index = index
        if raw.get("type") == _ASSISTANT_RECORD_TYPE:
            last_assistant_index = index
    return _TailFacts(
        has_interrupt_sentinel=last_sentinel_index >= 0,
        has_assistant_answer=last_assistant_index > last_sentinel_index,
    )


def poll_verdict(mirror_is_empty: bool, facts: _TailFacts, turn_came_alive: bool) -> TapVerdict | None:
    """The verdict on a single poll, or None to keep watching.

    A positive FLUSHED exits early on either signal that the flush started a turn: the flushed turn
    produced an answer, OR a turn has been seen alive at some poll since the chord (it started --
    it need not still be running now; a quick turn can start and finish inside the window). Both are
    gated on the mirror having drained. A drained mirror with neither signal yet is held: one may
    still land (FLUSHED), else the deadline resolves it as NEEDS_RECOVERY. A non-empty mirror is
    only ever NOT_FLUSHED, and only at the deadline. Keying the drain on the MIRROR (not on leaves
    in the tail) is deliberate: in the designed-for race claude's leaves land before the baseline,
    so an in-tail-leaves requirement would misread it as failure.
    """
    if mirror_is_empty and (facts.has_assistant_answer or turn_came_alive):
        return TapVerdict.FLUSHED
    return None


def deadline_verdict(mirror_is_empty: bool, facts: _TailFacts, turn_came_alive: bool) -> TapVerdict:
    """The verdict once the watch window elapses, from the accumulated observations.

    A drained mirror with a dangling interrupt sentinel and no answer is NEEDS_RECOVERY ONLY when a
    turn was NEVER seen alive at any poll in the window -- the flush committed the messages but
    nothing ran to answer them (the cancelled follow-on). If a turn WAS seen alive (even briefly --
    it may have started and finished inside the window), the flush went through; that is FLUSHED,
    not a cancelled follow-on. Keying on "a turn came alive" instead of "an answer arrived within
    the window" is what keeps the recovery nudge from firing on a fast or slow but successful flush.
    """
    if not mirror_is_empty:
        return TapVerdict.NOT_FLUSHED
    if facts.has_interrupt_sentinel and not facts.has_assistant_answer and not turn_came_alive:
        return TapVerdict.NEEDS_RECOVERY
    return TapVerdict.FLUSHED


def resolve_live_session_baseline(watcher: TapWatcher) -> tuple[Path, int] | None:
    """Resolve the live session file and its current byte size, or None if there is none.

    The baseline anchors the raw tail read after the chord. Shared with the sibling
    interrupt path.
    """
    session_file = watcher.get_latest_main_session_file()
    if session_file is None:
        return None
    try:
        return session_file, session_file.stat().st_size
    except OSError:
        return None


def _poll_tap_state(
    watcher: TapWatcher, session_file: Path, baseline_size: int, active_marker: Path
) -> tuple[bool, _TailFacts, bool]:
    """One observation: re-drive the mirror, classify the raw post-baseline tail, read liveness.

    Re-driving via ``get_all_events`` is the single queue-feed point, so a queue already
    flushed at natural turn end drains the mirror here. ``is_turn_active`` is the ``active``
    marker's presence -- claude's RUNNING signal (a live turn keeps it; a turn that ended
    clears it), read at the source mngr derives RUNNING/WAITING from. Returns
    ``(mirror_is_empty, facts, is_turn_active)``.
    """
    watcher.get_all_events()
    mirror_is_empty = len(watcher.get_queued_messages()) == 0
    facts = compute_tail_facts(read_raw_tail(session_file, baseline_size))
    is_turn_active = active_marker.exists()
    return mirror_is_empty, facts, is_turn_active


def watch_for_flush_verdict(
    watcher: TapWatcher,
    session_file: Path,
    baseline_size: int,
    active_marker: Path,
    *,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    watch_deadline_seconds: float = _WATCH_DEADLINE_SECONDS,
    poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
) -> TapVerdict:
    """Poll the mirror + raw tail + turn liveness until a terminal verdict or the deadline.

    ``turn_came_alive`` accumulates across polls (a turn seen alive at ANY poll stays remembered),
    so a turn that starts and finishes inside the window still counts as a successful flush. Exits
    early on a positive FLUSHED; otherwise polls to the deadline and finalizes from the accumulated
    observations. Shared with the sibling interrupt path.
    """
    deadline = now() + watch_deadline_seconds
    mirror_is_empty, facts, is_turn_active = _poll_tap_state(watcher, session_file, baseline_size, active_marker)
    turn_came_alive = is_turn_active
    while now() < deadline:
        verdict = poll_verdict(mirror_is_empty, facts, turn_came_alive)
        if verdict is not None:
            return verdict
        sleep(poll_interval_seconds)
        mirror_is_empty, facts, is_turn_active = _poll_tap_state(watcher, session_file, baseline_size, active_marker)
        turn_came_alive = turn_came_alive or is_turn_active
    verdict = poll_verdict(mirror_is_empty, facts, turn_came_alive)
    return verdict if verdict is not None else deadline_verdict(mirror_is_empty, facts, turn_came_alive)


def execute_claude_shoulder_tap(
    *,
    agent_state_dir: Path,
    keybindings_path: Path,
    watcher: TapWatcher,
    press_chord: Callable[[], bool],
    send_recovery: Callable[[str], bool],
    try_message_lock: Callable[[], AbstractContextManager[bool]],
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    watch_deadline_seconds: float = _WATCH_DEADLINE_SECONDS,
    poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
) -> ClaudeTapResult:
    """Flush a claude agent's parked queue into its live turn via the native cancel chord.

    ``press_chord`` delivers ``meta+q`` under mngr's per-agent lock (returns success);
    ``send_recovery`` sends a task-notification the same confirmed way a normal message is
    sent (returns success). ``try_message_lock`` is one bounded acquire of mngr's per-agent
    ``message.lock`` (yielding whether it was taken): the refresh-first mirror read runs under
    it so a tap racing a not-yet-parked send does not read an empty mirror and no-op the message
    away -- the same in-flight-send discipline pi's ``flush_pi_queue_atomic`` follows. All
    three are injected so the server wires them to the mngr message API and tests substitute
    fakes. See the module docstring for the flow and the never-loss bias of the watch.
    """
    # Baseline the tap's start so the recovery arm can tell whether a stop-button interrupt
    # (which delivers its own cancel chord) fired for this agent mid-watch -- see below.
    tap_started_at = now()

    # Self-provision the chord on upgraded workspaces: idempotent, and a no-op once bound.
    # (A process launched before the write still fails the gate until its next restart.)
    ensure_chat_cancel_tap_keybinding(keybindings_path)

    # Refresh-first UNDER the bounded ``message.lock`` (the codex/pi flush discipline): acquiring
    # the lock means any in-flight send has durably parked, so its text is already in the mirror
    # -- a tap racing a not-yet-parked send no longer reads an empty mirror and no-ops the message
    # away. A send still holding the lock past the bounded wait yields SEND_IN_FLIGHT: an explicit,
    # retryable refusal the endpoint surfaces (like codex/pi), never a silent NOTHING_QUEUED miss.
    # The lock is released here (before the gates and the chord): the chord's keypress re-acquires
    # it through mngr's locked message API, so holding it across ``press_chord`` would deadlock.
    with try_message_lock() as is_lock_held:
        if not is_lock_held:
            return ClaudeTapResult(
                status=ClaudeTapStatus.SEND_IN_FLIGHT,
                detail="A message send to the agent is in flight; try the shoulder tap again.",
            )
        # Drain a queue that already flushed at natural turn end, then read the settled mirror.
        watcher.get_all_events()
        mirror_is_empty = len(watcher.get_queued_messages()) == 0
    if mirror_is_empty:
        return ClaudeTapResult(status=ClaudeTapStatus.NOTHING_QUEUED)

    if not (agent_state_dir / ACTIVE_MARKER_FILENAME).exists():
        # No turn in flight, so there is nothing to cancel-and-flush; deliver no chord.
        return ClaudeTapResult(status=ClaudeTapStatus.NO_OPEN_TURN)

    if (agent_state_dir / PERMISSIONS_WAITING_MARKER_FILENAME).exists():
        # The Chat-only chord is inert on a dialog; fail fast rather than a guaranteed hang.
        return ClaudeTapResult(
            status=ClaudeTapStatus.PERMISSIONS_WAITING,
            detail="The agent is waiting on a dialog; try again once it is answered.",
        )

    process_marker = agent_state_dir / CLAUDE_PROCESS_STARTED_MARKER_FILENAME
    if not is_tap_binding_active(keybindings_path, process_marker):
        return ClaudeTapResult(
            status=ClaudeTapStatus.BINDING_NOT_ACTIVE,
            detail="The native shoulder-tap keybinding is not active yet; it takes effect on the agent's next restart.",
        )

    baseline = resolve_live_session_baseline(watcher)
    if baseline is None:
        # No live session file to observe -- nothing to flush into.
        return ClaudeTapResult(status=ClaudeTapStatus.NO_OPEN_TURN)
    session_file, baseline_size = baseline

    if not press_chord():
        return ClaudeTapResult(
            status=ClaudeTapStatus.CHORD_SEND_FAILED,
            detail="Failed to deliver the shoulder-tap chord to the agent.",
        )

    verdict = watch_for_flush_verdict(
        watcher,
        session_file,
        baseline_size,
        agent_state_dir / ACTIVE_MARKER_FILENAME,
        now=now,
        sleep=sleep,
        watch_deadline_seconds=watch_deadline_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    if verdict == TapVerdict.FLUSHED:
        return ClaudeTapResult(status=ClaudeTapStatus.TAPPED)
    if verdict == TapVerdict.NOT_FLUSHED:
        return ClaudeTapResult(
            status=ClaudeTapStatus.NOT_FLUSHED,
            detail="The queue did not flush within the expected window; nothing was resent.",
        )

    # NEEDS_RECOVERY: the flush committed the messages but no turn was ever seen alive to answer
    # them (a turn seen alive at any poll would have resolved to FLUSHED). Nudge the agent to address
    # the already-committed messages via the confirmed send path -- UNLESS a stop-button
    # interrupt fired for this agent since the tap began. A stop delivers its own cancel chord,
    # whose post-baseline sentinel is indistinguishable from the tap's own cancelled-follow-on
    # signature; resending here would re-drive the very turn the user just stopped. Suppress it.
    if _stop_ran_since(str(agent_state_dir), tap_started_at):
        logger.info("Suppressing claude shoulder-tap recovery: a stop interrupt ran during the tap watch")
        return ClaudeTapResult(status=ClaudeTapStatus.TAPPED)
    logger.info("Claude shoulder tap cancelled a flushed follow-on turn; sending recovery message")
    if not send_recovery(RECOVERY_MESSAGE):
        return ClaudeTapResult(
            status=ClaudeTapStatus.RECOVERY_SEND_FAILED,
            detail="The queued messages were delivered but the recovery nudge could not be sent.",
        )
    return ClaudeTapResult(status=ClaudeTapStatus.TAPPED)


# =============================================================================
# Stop button (Contract B), empty-queue case: the same chord, resolved as a pure interrupt.
# =============================================================================


class _AbortVerdict(StrEnum):
    """The terminal reading of the post-chord abort watch (the stop button's verdict lattice).

    - CONFIRMED: a post-baseline interrupt sentinel is on disk -- the chord aborted the turn.
    - TURN_ENDED: the ``active`` marker vanished with no sentinel -- the turn ended naturally in
      the gap and its own Stop hook cleared the marker; the chord was a no-op.
    - UNCONFIRMED: the deadline passed with the marker still present and no sentinel -- the chord
      may have been eaten (an ungated dialog state); fall back to the base restart-drain.
    """

    CONFIRMED = "confirmed"
    TURN_ENDED = "turn_ended"
    UNCONFIRMED = "unconfirmed"


def _is_interrupt_abort_record(raw: dict[str, Any]) -> bool:
    """True iff ``raw`` is a user record whose text is an interrupt sentinel (either shape).

    Pinned to the PARSED user-record shape, never a raw substring: ``extract_text_content``
    reads only ``text`` blocks, so a ``tool_result`` quoting the sentinel (routine when an agent
    greps its own session JSONL) yields empty text and cannot false-confirm the abort -- the
    exact inversion the confirm-before-clear ordering exists to prevent.
    """
    if raw.get("type") != _USER_RECORD_TYPE:
        return False
    message = raw.get("message")
    if not isinstance(message, dict):
        return False
    return is_interrupt_sentinel_text(extract_text_content(message.get("content")))


def _tail_has_interrupt_abort(tail_lines: list[str]) -> bool:
    """True iff any complete raw line in the post-baseline tail is an interrupt-abort record."""
    for line in tail_lines:
        raw = _load_json_object(line)
        if raw is not None and _is_interrupt_abort_record(raw):
            return True
    return False


def _poll_abort_verdict(session_file: Path, baseline_size: int, active_marker: Path) -> _AbortVerdict | None:
    """One observation of the abort watch, or None to keep watching.

    Reads BOTH signals: a post-baseline interrupt sentinel (CONFIRMED, the fast exit) and the
    ``active`` marker's presence (its absence with no sentinel is TURN_ENDED, the turn ended
    naturally). Neither present -> None (keep polling until the deadline).
    """
    if _tail_has_interrupt_abort(read_raw_tail(session_file, baseline_size)):
        return _AbortVerdict.CONFIRMED
    if not active_marker.exists():
        return _AbortVerdict.TURN_ENDED
    return None


def watch_for_abort_verdict(
    session_file: Path,
    baseline_size: int,
    agent_state_dir: Path,
    *,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    watch_deadline_seconds: float = _INTERRUPT_WATCH_DEADLINE_SECONDS,
    poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
) -> _AbortVerdict:
    """Poll the raw post-baseline tail + the ``active`` marker until a terminal abort verdict.

    Reuses the tap's baseline + raw-tail reader; only the verdict differs (see
    :func:`_poll_abort_verdict`). Mirrors the tap's ``watch_for_flush_verdict`` idiom -- an
    initial poll, then poll until a verdict or the deadline -- and resolves a still-present
    marker with no sentinel as UNCONFIRMED. Shared clock/sleep are injected for tests.
    """
    active_marker = agent_state_dir / ACTIVE_MARKER_FILENAME
    deadline = now() + watch_deadline_seconds
    verdict = _poll_abort_verdict(session_file, baseline_size, active_marker)
    while verdict is None and now() < deadline:
        sleep(poll_interval_seconds)
        verdict = _poll_abort_verdict(session_file, baseline_size, active_marker)
    return verdict if verdict is not None else _AbortVerdict.UNCONFIRMED


def _no_in_flight_block() -> str:
    """Default in-flight-Sending source: nothing recorded (tests and legacy callers)."""
    return ""


def _no_settle_activity() -> None:
    """Default activity-settle: a no-op (unit tests that assert only the block/markers)."""
    return None


def _combine_return_block(queued_block: str, in_flight_block: str) -> str:
    """Concatenate the queued block and the in-flight (Sending) block, in send order.

    Queued messages (parked first) lead; a message still mid-send follows. Either may be
    empty; the result drops the empties so an empty queue or no in-flight send does not
    inject a blank line. Matches the queued block's own newline join so the composer sees
    one uniform block.
    """
    return "\n".join(part for part in (queued_block, in_flight_block) if part)


def _drain_to_base_under_message_lock(
    watcher: TapWatcher,
    restart_drain_to_base: Callable[[], str],
    try_message_lock: Callable[[], AbstractContextManager[bool]],
    get_in_flight_block: Callable[[], str] = _no_in_flight_block,
) -> str:
    """Refresh the mirror and run the base restart-drain under ONE bounded message-lock acquire.

    Acquiring the lock means any in-flight send has durably resolved (committed, queued, or
    failed) and released, so a message that parked between the caller's earlier mirror read and
    the SIGKILL rides the returned queued block instead of dying silently with the process
    (message conservation). When the bounded wait EXPIRES the lock is still held by an in-flight
    send that has NOT committed -- the SIGKILL will abort it -- so that Sending message is folded
    into the returned block (contract A4/B: return every not-Delivered message), reconciled per id
    by the Sending registry (a committed/queued send has already cleared its own record, so a lock
    held past the wait means the record is genuinely unresolved). When the lock IS acquired we do
    NOT add the in-flight block: the send resolved, so its record is (being) cleared and adding it
    would risk returning an already-committed message.
    """
    with try_message_lock() as is_lock_held:
        watcher.get_all_events()
        queued_block = restart_drain_to_base()
        if is_lock_held:
            return queued_block
        logger.info(
            "claude stop: message.lock still held past the bounded wait; aborting the in-flight send and "
            "returning it to the composer with the queued block"
        )
        return _combine_return_block(queued_block, get_in_flight_block())


def execute_claude_stop_to_composer(
    *,
    agent_state_dir: Path,
    keybindings_path: Path,
    watcher: TapWatcher,
    press_chord: Callable[[], bool],
    mark_idle: Callable[[], None],
    settle_activity: Callable[[], None] = _no_settle_activity,
    restart_drain_to_base: Callable[[], str],
    try_message_lock: Callable[[], AbstractContextManager[bool]],
    get_in_flight_block: Callable[[], str] = _no_in_flight_block,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    watch_deadline_seconds: float = _INTERRUPT_WATCH_DEADLINE_SECONDS,
    poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
) -> str:
    """Interrupt a claude turn on an EMPTY queue via the native cancel chord; return the block.

    Branches on the queue mirror (the same never-branch-on-harness dispatch the endpoint uses):

    - Mirror NONEMPTY -> the base restart-drain: it both interrupts and hands the queued block
      back (a chord here would COMMIT the very messages stop promises to retract), with the
      mirror refreshed and the block captured under the bounded ``message.lock`` -- so a message
      an in-flight send parked since the pre-lock read rides the block instead of dying with the
      SIGKILL (U1); a lock still held past the wait hammers on a fresh best-effort re-capture.
    - Mirror EMPTY, no ``active`` marker -> ``""``: no turn, nothing queued; composer untouched.
    - Mirror EMPTY, a dialog / inactive binding / no live session -> the base (same bounded-lock
      capture): the Chat-only chord is inert or unobservable, but a blocked turn is still a turn.
    - Mirror EMPTY + open turn + bindable -> the chord: re-check under the bounded lock (a
      mid-flight send that parked while we waited routes back to the base, captured under that
      same hold; a lock still held past the wait routes straight to the hammer instead of
      stalling the stop behind the send's turn-confirm), deliver ``meta+q``, then watch.
      CONFIRMED -> settle the activity indicator via ``settle_activity`` (one direct broadcast,
      so the dot clears at once) AND mark the agent idle (claude fires no hook on interrupt,
      stranding ``active``), then return ``""``; TURN_ENDED -> return ``""``, clear nothing;
      UNCONFIRMED -> the base.

    Whenever the bounded ``message.lock`` acquire EXPIRES -- a send is still in flight and has not
    committed -- the aborted Sending message is returned to the composer alongside the queued block
    (contract A4/B: return every not-Delivered message). ``get_in_flight_block`` supplies that text
    (the Sending records the send endpoint keeps on the watcher); it is folded in ONLY on the
    lock-not-held branches, because acquiring the lock means the send resolved (committed/queued)
    and cleared its own record, so adding it there would risk returning an already-committed
    message. On the chord path (lock acquired, mirror empty) the block is therefore empty. Every
    branch that carries a real block goes through ``restart_drain_to_base``. ``press_chord`` /
    ``mark_idle`` / ``settle_activity`` / ``restart_drain_to_base`` / ``try_message_lock`` /
    ``get_in_flight_block`` are injected so the override wires the real mngr boundaries (the
    endpoint's ``reset_activity_state`` for ``settle_activity``) and tests substitute fakes; each
    ``try_message_lock()`` call is one bounded acquire of mngr's per-agent ``message.lock``
    (waiting at most the shared ``STOP_LOCK_WAIT_SECONDS``), yielding whether the lock was taken.
    """
    # Self-provision the chord binding (idempotent, a no-op once bound), like the tap.
    ensure_chat_cancel_tap_keybinding(keybindings_path)

    # Refresh-first, then read the mirror.
    watcher.get_all_events()
    if len(watcher.get_queued_messages()) > 0:
        return _drain_to_base_under_message_lock(watcher, restart_drain_to_base, try_message_lock, get_in_flight_block)

    # Mirror is empty from here on.
    if not (agent_state_dir / ACTIVE_MARKER_FILENAME).exists():
        # No turn in flight and nothing queued: a pure no-op.
        return ""

    if (agent_state_dir / PERMISSIONS_WAITING_MARKER_FILENAME).exists():
        # The Chat-only chord is inert under a dialog, but a blocked turn is still a turn.
        return _drain_to_base_under_message_lock(watcher, restart_drain_to_base, try_message_lock, get_in_flight_block)

    process_marker = agent_state_dir / CLAUDE_PROCESS_STARTED_MARKER_FILENAME
    if not is_tap_binding_active(keybindings_path, process_marker):
        # The chord is not live for this process yet; the base restart still interrupts.
        return _drain_to_base_under_message_lock(watcher, restart_drain_to_base, try_message_lock, get_in_flight_block)

    baseline = resolve_live_session_baseline(watcher)
    if baseline is None:
        # No live session file to observe the abort against; fall back so stop still works.
        return _drain_to_base_under_message_lock(watcher, restart_drain_to_base, try_message_lock, get_in_flight_block)
    session_file, baseline_size = baseline

    # Under-lock re-check, BOUNDED: an in-flight ``mngr message`` send holds ``message.lock``
    # through its whole paste-and-confirm cycle, so acquiring it means any such send has durably
    # parked. Re-run the empty-mirror steps under the lock: a send that filled the mirror while
    # we waited routes to the base (captured under this very hold, so the just-parked message
    # rides the returned block) instead of being chord-flushed; a turn that ended is a no-op.
    # When the wait expires (an idle-start send in its turn-confirm), the chord cannot be
    # ordered against the send, so take the hammer directly -- without re-acquiring the
    # contended lock. (On the chord path the lock is released before the chord is pressed --
    # ``press_chord`` re-acquires it -- which is the accepted capture-window residual.)
    with try_message_lock() as is_lock_held:
        if not is_lock_held:
            logger.info(
                "claude stop: message.lock still held past the bounded wait; aborting the in-flight send and "
                "returning it to the composer"
            )
            watcher.get_all_events()
            # The lock is still held by a send that has NOT committed; the hammer will abort it,
            # so fold that Sending message into the returned block (A4/B). The mirror is empty here,
            # so the base block is empty and the in-flight block is what returns.
            return _combine_return_block(restart_drain_to_base(), get_in_flight_block())
        watcher.get_all_events()
        if len(watcher.get_queued_messages()) > 0:
            return restart_drain_to_base()
        if not (agent_state_dir / ACTIVE_MARKER_FILENAME).exists():
            return ""

    # Record the stop BEFORE delivering the chord so a tap watching this same agent suppresses
    # its recovery resend (the chord's sentinel would otherwise read as the tap's own follow-on
    # cancel and re-drive the just-stopped turn).
    _record_stop(str(agent_state_dir), now=now)

    if not press_chord():
        # The chord could not be delivered; the base restart still interrupts (stop must work).
        return _drain_to_base_under_message_lock(watcher, restart_drain_to_base, try_message_lock, get_in_flight_block)

    verdict = watch_for_abort_verdict(
        session_file,
        baseline_size,
        agent_state_dir,
        now=now,
        sleep=sleep,
        watch_deadline_seconds=watch_deadline_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    if verdict == _AbortVerdict.CONFIRMED:
        # The abort is on disk. Settle the activity indicator with ONE direct broadcast --
        # the SAME ``reset_activity_state`` the restart-drain path runs -- so the dot clears at
        # once (A6: model done -> dot cleared immediately), rather than only indirectly via the
        # observe re-probe ``mark_idle`` pokes (a two-hop, laggy chain). Then clear the stranded
        # on-disk markers so a later observe also reads idle and never re-derives THINKING. The
        # mirror was empty, so the block is empty. Confirm-before-clear: markers move only here.
        # Both are best-effort: the interrupt already succeeded, so a settle/marker-cleanup
        # failure must not turn a completed stop into a 500 -- log and still return "".
        try:
            settle_activity()
        except (ConcurrencyGroupError, OSError) as e:
            logger.opt(exception=e).warning(
                "claude stop: abort confirmed but settling activity failed; indicator will lag"
            )
        try:
            mark_idle()
        except (ConcurrencyGroupError, OSError) as e:
            logger.opt(exception=e).warning("claude stop: abort confirmed but marking idle failed; indicator will lag")
        return ""
    if verdict == _AbortVerdict.TURN_ENDED:
        # The turn ended naturally in the gap and its own Stop hook settled it; nothing to do.
        return ""
    # UNCONFIRMED: fall back to the base so the turn is definitely interrupted.
    return _drain_to_base_under_message_lock(watcher, restart_drain_to_base, try_message_lock, get_in_flight_block)


class ClaudeInterruptToComposer(InterruptToComposer):
    """claude's stop button: chord-interrupt an EMPTY queue, restart-drain a NONEMPTY one.

    Registered on the claude :class:`~harnesses.registry.HarnessSpec`. Delegates the whole
    verdict path to :func:`execute_claude_stop_to_composer`, wiring the real mngr boundaries: the
    injected ``press_chord`` (endpoint-bound to mngr's locked keypress), the mngr_claude
    ``mark_claude_agent_idle`` primitive (the hooks' own idle-marking, called in-process like the
    keypress), the shared ``restart_drain`` for every base delegation, and the BOUNDED per-agent
    ``message.lock`` acquire (``try_hold_message_lock``, a fresh acquire per call) for the
    under-lock re-check and every base capture. See the module docstring and the executor for
    the branch table.
    """

    _agent_info: AgentInfo
    _agent_state_dir: Path
    _keybindings_path: Path

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "ClaudeInterruptToComposer":
        self = cls.__new__(cls)
        self._agent_info = agent_info
        self._agent_state_dir = agent_info.agent_state_dir
        self._keybindings_path = agent_info.claude_config_dir / KEYBINDINGS_FILENAME
        return self

    def drain_to_composer(
        self,
        watcher: AgentSessionWatcher,
        restart_process: RestartProcess,
        settle_activity: SettleActivity,
        press_chord: PressChord,
        get_in_flight_block: Callable[[], str],
    ) -> str:
        return execute_claude_stop_to_composer(
            agent_state_dir=self._agent_state_dir,
            keybindings_path=self._keybindings_path,
            watcher=watcher,
            press_chord=press_chord,
            mark_idle=lambda: mark_claude_agent_idle(self._agent_state_dir, get_host_dir()),
            settle_activity=settle_activity,
            restart_drain_to_base=lambda: restart_drain(self._agent_info, watcher, restart_process, settle_activity),
            try_message_lock=lambda: try_hold_message_lock(self._agent_state_dir),
            get_in_flight_block=get_in_flight_block,
        )


class ClaudeAtomicShoulderTap(AtomicShoulderTap):
    """Claude's native tap: cancel the live turn via the chat-cancel chord, then recover.

    The keypress and the recovery send both route through the agent manager (which delegates
    to mngr's in-process message API, holding the per-agent ``message.lock``): dwt never
    drives raw tmux. The refresh-first mirror read is taken under a bounded acquire of that
    same lock, so a tap racing a not-yet-parked send is refused with ``send_in_flight``
    rather than no-oping it away -- the codex/pi discipline. Terminal no-op / success
    statuses return as plain outcomes; a dialog block maps to 409; every other error
    (including ``send_in_flight``, a retryable refusal) maps to 500.
    """

    _agent_state_dir: Path
    _keybindings_path: Path

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "ClaudeAtomicShoulderTap":
        self = cls.__new__(cls)
        self._agent_state_dir = agent_info.agent_state_dir
        self._keybindings_path = agent_info.claude_config_dir / KEYBINDINGS_FILENAME
        return self

    def tap(
        self,
        watcher: AgentSessionWatcher,
        press_chord: Callable[[], bool],
        send_recovery: Callable[[str], bool],
    ) -> ShoulderTapOutcome:
        result = execute_claude_shoulder_tap(
            agent_state_dir=self._agent_state_dir,
            keybindings_path=self._keybindings_path,
            watcher=watcher,
            press_chord=press_chord,
            send_recovery=send_recovery,
            try_message_lock=lambda: try_hold_message_lock(self._agent_state_dir),
        )
        if result.status in OK_TAP_STATUSES:
            return ShoulderTapOutcome(status=result.status.value)
        return ShoulderTapOutcome(
            status=result.status.value,
            error_detail=result.detail,
            error_status_code=409 if result.status == ClaudeTapStatus.PERMISSIONS_WAITING else 500,
        )

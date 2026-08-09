"""Claude's native shoulder tap: flush the parked message queue into the live turn.

Contract C (see docs/design/queue_sweep) for claude, WITHOUT the SIGKILL-restart the
base ``/flush-queue`` path uses. Cancelling claude's live turn makes it flush its parked
queue through immediately -- the same auto-flush it performs at natural turn end. We
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

The watch is biased toward *never losing* a message: its only fast exit is a positive
FLUSHED signal (the mirror drained and the flushed turn produced an answer). A drained
mirror with a dangling interrupt sentinel (the chord cancelled the flushed follow-on turn)
is held to the deadline and resolved as NEEDS_RECOVERY, and a mirror that never drains as
NOT_FLUSHED -- so the accepted failure modes are a spurious recovery message or a spurious
500 (both visible), never a silently stranded queue.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any
from typing import Protocol

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_claude.claude_config import ensure_chat_cancel_tap_keybinding
from imbue.mngr_claude.claude_config import is_tap_binding_active
from imbue.system_interface.activity_state import ACTIVE_MARKER_FILENAME
from imbue.system_interface.harnesses.claude.session_parser import INTERRUPT_SENTINEL_TEXT
from imbue.system_interface.harnesses.claude.session_parser import extract_text_content

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

    - FLUSHED: mirror drained and the flushed turn produced an answer (or there was nothing to recover).
    - NEEDS_RECOVERY: mirror drained but a post-baseline interrupt sentinel has no answer after it -- the
      chord cancelled the follow-on turn the flush started, so the committed messages need a nudge.
    - NOT_FLUSHED: the deadline passed with the mirror still non-empty; the flush did not go through.
    """

    FLUSHED = "flushed"
    NEEDS_RECOVERY = "needs_recovery"
    NOT_FLUSHED = "not_flushed"


class ClaudeTapStatus(StrEnum):
    """The executor's outcome; the server maps it to an HTTP response.

    The 200 no-ops are NOTHING_QUEUED (mirror already empty after refresh) and NO_OPEN_TURN
    (no turn in flight, so no chord delivered); TAPPED is the 200 success (flushed, and on the
    race its recovery message sent). The rest are errors the server surfaces: PERMISSIONS_WAITING
    (409, claude is on a dialog the chord is inert in), BINDING_NOT_ACTIVE (500, the chord is not
    live for this process yet), CHORD_SEND_FAILED / RECOVERY_SEND_FAILED (500, a delivery failed),
    and NOT_FLUSHED (500, the queue did not flush within the watch window).
    """

    NOTHING_QUEUED = "nothing_queued"
    NO_OPEN_TURN = "no_open_turn"
    PERMISSIONS_WAITING = "permissions_waiting"
    BINDING_NOT_ACTIVE = "binding_not_active"
    CHORD_SEND_FAILED = "chord_send_failed"
    NOT_FLUSHED = "not_flushed"
    RECOVERY_SEND_FAILED = "recovery_send_failed"
    TAPPED = "tapped"


# The statuses that map to a 200 (a successful tap or an idempotent no-op).
OK_TAP_STATUSES: frozenset[ClaudeTapStatus] = frozenset(
    {ClaudeTapStatus.NOTHING_QUEUED, ClaudeTapStatus.NO_OPEN_TURN, ClaudeTapStatus.TAPPED}
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


def poll_verdict(mirror_is_empty: bool, facts: _TailFacts) -> TapVerdict | None:
    """The verdict on a single poll, or None to keep watching.

    Only a positive FLUSHED (the mirror drained AND the flushed turn produced an answer)
    exits early. A drained mirror with no answer yet is held: an answer may still land
    (FLUSHED), else the deadline resolves it as NEEDS_RECOVERY. A non-empty mirror is only
    ever NOT_FLUSHED, and only at the deadline. Keying the drain on the MIRROR (not on
    leaves in the tail) is deliberate: in the designed-for race claude's leaves land before
    the baseline, so an in-tail-leaves requirement would misread it as failure.
    """
    if mirror_is_empty and facts.has_assistant_answer:
        return TapVerdict.FLUSHED
    return None


def deadline_verdict(mirror_is_empty: bool, facts: _TailFacts) -> TapVerdict:
    """The verdict once the watch window elapses, from the last poll's observations."""
    if not mirror_is_empty:
        return TapVerdict.NOT_FLUSHED
    if facts.has_interrupt_sentinel and not facts.has_assistant_answer:
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


def _poll_tap_state(watcher: TapWatcher, session_file: Path, baseline_size: int) -> tuple[bool, _TailFacts]:
    """One observation: re-drive the mirror, then classify the raw post-baseline tail.

    Re-driving via ``get_all_events`` is the single queue-feed point, so a queue already
    flushed at natural turn end drains the mirror here. Returns ``(mirror_is_empty, facts)``.
    """
    watcher.get_all_events()
    mirror_is_empty = len(watcher.get_queued_messages()) == 0
    facts = compute_tail_facts(read_raw_tail(session_file, baseline_size))
    return mirror_is_empty, facts


def watch_for_flush_verdict(
    watcher: TapWatcher,
    session_file: Path,
    baseline_size: int,
    *,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    watch_deadline_seconds: float = _WATCH_DEADLINE_SECONDS,
    poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
) -> TapVerdict:
    """Poll the mirror + raw tail until a terminal verdict or the deadline.

    Exits early only on a positive FLUSHED; otherwise keeps polling until the deadline and
    finalizes from the last observation. Shared with the sibling interrupt path.
    """
    deadline = now() + watch_deadline_seconds
    mirror_is_empty, facts = _poll_tap_state(watcher, session_file, baseline_size)
    while now() < deadline:
        verdict = poll_verdict(mirror_is_empty, facts)
        if verdict is not None:
            return verdict
        sleep(poll_interval_seconds)
        mirror_is_empty, facts = _poll_tap_state(watcher, session_file, baseline_size)
    verdict = poll_verdict(mirror_is_empty, facts)
    return verdict if verdict is not None else deadline_verdict(mirror_is_empty, facts)


def execute_claude_shoulder_tap(
    *,
    agent_state_dir: Path,
    keybindings_path: Path,
    watcher: TapWatcher,
    press_chord: Callable[[], bool],
    send_recovery: Callable[[str], bool],
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    watch_deadline_seconds: float = _WATCH_DEADLINE_SECONDS,
    poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
) -> ClaudeTapResult:
    """Flush a claude agent's parked queue into its live turn via the native cancel chord.

    ``press_chord`` delivers ``meta+q`` under mngr's per-agent lock (returns success);
    ``send_recovery`` sends a task-notification the same confirmed way a normal message is
    sent (returns success). Both are injected so the server wires them to the mngr message
    API and tests substitute fakes. See the module docstring for the flow and the never-loss
    bias of the watch.
    """
    # Self-provision the chord on upgraded workspaces: idempotent, and a no-op once bound.
    # (A process launched before the write still fails the gate until its next restart.)
    ensure_chat_cancel_tap_keybinding(keybindings_path)

    # Refresh-first: drain a queue that already flushed at natural turn end before gating.
    watcher.get_all_events()
    if len(watcher.get_queued_messages()) == 0:
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

    # NEEDS_RECOVERY: the chord cancelled the flushed follow-on turn. Nudge the agent to
    # address the already-committed messages via the confirmed send path.
    logger.info("Claude shoulder tap cancelled a flushed follow-on turn; sending recovery message")
    if not send_recovery(RECOVERY_MESSAGE):
        return ClaudeTapResult(
            status=ClaudeTapStatus.RECOVERY_SEND_FAILED,
            detail="The queued messages were delivered but the recovery nudge could not be sent.",
        )
    return ClaudeTapResult(status=ClaudeTapStatus.TAPPED)

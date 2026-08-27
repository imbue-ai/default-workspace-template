from typing import Any

import pytest

from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.harnesses.claude.activity import ClaudeActivityTracker
from imbue.system_interface.harnesses.codex.activity import CodexActivityTracker
from imbue.system_interface.harnesses.events import SPECIAL_EVENT_TYPE
from imbue.system_interface.harnesses.events import SpecialEventKind
from imbue.system_interface.harnesses.harness_type import DEFAULT_HARNESS
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.harness_type import parse_harness
from imbue.system_interface.harnesses.pi_coding.activity import PiActivityTracker
from imbue.system_interface.harnesses.registry import build_tracker
from imbue.system_interface.harnesses.registry import get_harness_spec

# Every parser here emits the same common event schema (tool calls nested in
# ``assistant_message``, results keyed by ``tool_call_id`` -- see ``harnesses/events.py``), so
# these harnesses run the shared tests. The fixtures open with a ``turn_started`` marker so
# codex's turn latch engages; claude/pi read the tail and ignore the marker.
#
# NOT ``tuple(HarnessType)``: every assertion below reads a state INFERRED FROM A TRANSCRIPT,
# which the launch-only harnesses (see ``_PLACEHOLDER_HARNESSES``) do not have. Their dot comes
# from mngr's ``active`` marker alone, so they can never report TOOL_RUNNING and cannot have a
# stale tail. Covering them here would mean weakening these assertions for the harnesses that
# CAN meet them; they get their own test instead. Move a harness up to this tuple when it lands
# a real transcript watcher.
_TRACKER_HARNESSES = (HarnessType.CLAUDE, HarnessType.CODEX, HarnessType.PI_CODING, HarnessType.ANTIGRAVITY)

# The harnesses on the shared placeholder watcher/tracker -- launchable, with no transcript
# behind them. Emptying this tuple is what retires ``harnesses/placeholder.py``. antigravity
# graduated out of it once its real watcher landed; it still uses the placeholder RESOLVER,
# which is why its catalog is asserted empty below rather than here.
_PLACEHOLDER_HARNESSES = (HarnessType.OPENCODE,)


def _turn_started_marker() -> dict[str, Any]:
    return {"type": SPECIAL_EVENT_TYPE, "kind": SpecialEventKind.TURN_STARTED.value}


@pytest.mark.parametrize(
    "harness, expected_type, expected_marker",
    [
        pytest.param(HarnessType.CLAUDE, ClaudeActivityTracker, "claude_process_started", id="claude"),
        pytest.param(HarnessType.PI_CODING, PiActivityTracker, "pi_process_started", id="pi"),
    ],
)
def test_build_tracker(harness: HarnessType, expected_type: type, expected_marker: str) -> None:
    tracker = build_tracker(harness)
    assert isinstance(tracker, expected_type)
    # Each harness reads the marker its own mngr plugin writes -- the pairing
    # that used to be a hardcoded claude filename in AgentManager.
    assert tracker.marker_filename == expected_marker


def test_codex_builds_a_turn_latch_activity_tracker() -> None:
    """Codex registers a transcript-derived tracker like claude/pi. Its dot is a latch on the
    transcript's turn markers (the mngr lifecycle is deliberately not consulted -- laggy for
    codex); the ledger stays for the queue only."""
    assert get_harness_spec(HarnessType.CODEX).tracker_class is CodexActivityTracker
    tracker = build_tracker(HarnessType.CODEX)
    assert isinstance(tracker, CodexActivityTracker)
    assert tracker.marker_filename == "codex_process_started"


@pytest.mark.parametrize("agent_type", ["wait", "main", "", None], ids=["wait", "main", "empty", "none"])
def test_parse_harness_narrows_a_non_harness_agent_type(agent_type: str | None) -> None:
    """mngr agent types that are not harnesses keep the claude derivation rather than
    losing the indicator -- and they are narrowed HERE, so every lookup downstream is total."""
    assert parse_harness(agent_type) is DEFAULT_HARNESS
    assert isinstance(build_tracker(parse_harness(agent_type)), ClaudeActivityTracker)


def test_every_harness_has_a_spec() -> None:
    """A new HarnessType member with no spec is a KeyError at lookup time; catch it here."""
    for harness in HarnessType:
        assert get_harness_spec(harness) is not None


def test_each_tracker_is_independent() -> None:
    """Trackers are per-agent, so one agent's signals never leak into another's."""
    first = build_tracker(HarnessType.CLAUDE)
    second = build_tracker(HarnessType.CLAUDE)
    assert first.observe([{"type": "user_message", "timestamp": "2026-07-28T00:00:00Z"}]) is True
    assert first.derive(lifecycle_state="RUNNING", is_active_marker_present=False, process_started_at=None) == ActivityState.THINKING
    assert second.derive(lifecycle_state="RUNNING", is_active_marker_present=False, process_started_at=None) == ActivityState.IDLE


@pytest.mark.parametrize("harness", _TRACKER_HARNESSES)
def test_fresh_tracker_is_idle(harness: HarnessType) -> None:
    tracker = build_tracker(harness)
    assert tracker.derive(lifecycle_state="RUNNING", is_active_marker_present=False, process_started_at=None) == ActivityState.IDLE


@pytest.mark.parametrize("harness", _TRACKER_HARNESSES)
def test_observe_reports_no_change_on_repeat(harness: HarnessType) -> None:
    """A repeated event list must short-circuit, so streamed lines stay cheap."""
    events: list[dict[str, Any]] = [_turn_started_marker(), {"type": "user_message", "timestamp": "2026-07-28T00:00:00Z"}]
    tracker = build_tracker(harness)
    assert tracker.observe(events) is True, f"{harness} should register the first event"
    assert tracker.observe(events) is False, f"{harness} should short-circuit an unchanged list"


def test_claude_honors_the_mngr_lifecycle() -> None:
    """A stopped claude agent is IDLE regardless of a mid-turn transcript tail."""
    tracker = build_tracker(HarnessType.CLAUDE)
    tracker.observe([{"type": "user_message", "timestamp": "2026-07-28T00:00:00Z"}])
    assert tracker.derive(lifecycle_state="RUNNING", is_active_marker_present=False, process_started_at=None) == ActivityState.THINKING
    assert tracker.derive(lifecycle_state="WAITING", is_active_marker_present=False, process_started_at=None) == ActivityState.IDLE


@pytest.mark.parametrize("harness", _TRACKER_HARNESSES)
def test_stale_transcript_tail_reads_idle(harness: HarnessType) -> None:
    """A turn abandoned by a prior process must not pin the indicator.

    The tail predates the current process's marker mtime, so the staleness
    override fires. An absent marker yields process_started_at=None, which
    disables the override entirely.
    """
    tracker = build_tracker(harness)
    tracker.observe(
        [
            _turn_started_marker(),
            {"type": "user_message", "timestamp": "2026-07-28T00:00:00Z"},
        ]
    )
    stale_tail_is_live = tracker.derive(lifecycle_state="RUNNING", is_active_marker_present=False, process_started_at=None)
    assert stale_tail_is_live != ActivityState.IDLE

    # Marker touched after the tail -> the turn belongs to a dead process.
    # 2100-01-01, comfortably after the tail above.
    restarted_at = 4102444800.0
    assert tracker.derive(lifecycle_state="RUNNING", is_active_marker_present=False, process_started_at=restarted_at) == ActivityState.IDLE


@pytest.mark.parametrize("harness", _TRACKER_HARNESSES)
def test_reset_settles_on_idle(harness: HarnessType) -> None:
    tracker = build_tracker(harness)
    tracker.observe(
        [
            _turn_started_marker(),
            {"type": "user_message", "timestamp": "2026-07-28T00:00:00Z"},
        ]
    )
    assert tracker.derive(lifecycle_state="RUNNING", is_active_marker_present=False, process_started_at=None) != ActivityState.IDLE
    tracker.reset()
    assert tracker.derive(lifecycle_state="RUNNING", is_active_marker_present=False, process_started_at=None) == ActivityState.IDLE


@pytest.mark.parametrize("harness", _TRACKER_HARNESSES)
def test_pending_tool_use_reads_tool_running(harness: HarnessType) -> None:
    tracker = build_tracker(harness)
    tracker.observe(
        [
            _turn_started_marker(),
            {
                "type": "assistant_message",
                "timestamp": "2026-07-28T00:00:01Z",
                "tool_calls": [{"tool_call_id": "call-1", "tool_name": "Bash"}],
            },
        ]
    )
    assert tracker.derive(lifecycle_state="RUNNING", is_active_marker_present=False, process_started_at=None) == ActivityState.TOOL_RUNNING


@pytest.mark.parametrize("harness", _TRACKER_HARNESSES)
def test_dead_lifecycle_settles_idle_for_every_harness(harness: HarnessType) -> None:
    """The dead gate is the BASE's own first step, structurally applied to every harness.

    This matters most for codex, whose working derivation deliberately ignores the mngr
    lifecycle (the turn latch is authoritative): without the base gate, a mid-turn daemon
    kill would leave its open turn pinning the dot forever.
    """
    tracker = build_tracker(harness)
    tracker.observe(
        [
            _turn_started_marker(),
            {
                "type": "assistant_message",
                "timestamp": "2026-07-28T00:00:01Z",
                "tool_calls": [{"tool_call_id": "call-1", "tool_name": "Bash"}],
            },
        ]
    )
    live = tracker.derive(lifecycle_state="RUNNING", is_active_marker_present=False, process_started_at=None)
    assert live == ActivityState.TOOL_RUNNING
    dead = tracker.derive(lifecycle_state="STOPPED", is_active_marker_present=False, process_started_at=None)
    assert dead == ActivityState.IDLE


def test_active_marker_declarations_match_what_mngr_writes() -> None:
    """claude/pi keep the shared `active` marker their hooks/extension flip; codex declares
    None (mngr_codex writes no marker -- the daemon is the turn authority)."""
    assert build_tracker(HarnessType.CLAUDE).active_marker_filename == "active"
    assert build_tracker(HarnessType.PI_CODING).active_marker_filename == "active"
    assert build_tracker(HarnessType.CODEX).active_marker_filename is None


@pytest.mark.parametrize("harness", _PLACEHOLDER_HARNESSES)
def test_placeholder_harness_dot_follows_the_active_marker(harness: HarnessType) -> None:
    """A launch-only harness derives its dot from mngr's ``active`` marker and nothing else.

    Both plugins behind these harnesses maintain that marker already, so the dot is live even
    with no transcript. The states it can reach are exactly two: THINKING while a turn is in
    flight, IDLE otherwise -- never TOOL_RUNNING, which needs an unmatched tool call from a
    transcript. Pinned here so swapping in a real tracker is a deliberate, visible change.
    """
    tracker = build_tracker(harness)
    assert tracker.derive(lifecycle_state="RUNNING", is_active_marker_present=False, process_started_at=None) == ActivityState.IDLE
    assert tracker.derive(lifecycle_state="RUNNING", is_active_marker_present=True, process_started_at=None) == ActivityState.THINKING
    # The base class's dead-lifecycle gate still applies, exactly as it does for every harness.
    assert tracker.derive(lifecycle_state="STOPPED", is_active_marker_present=True, process_started_at=None) == ActivityState.IDLE


@pytest.mark.parametrize("harness", _PLACEHOLDER_HARNESSES)
def test_placeholder_harness_reports_an_empty_transcript(harness: HarnessType) -> None:
    """The placeholder watcher/catalog answer empty rather than raising, so a launch-only
    harness renders a blank chat instead of erroring, and its model bar shows nothing."""
    spec = get_harness_spec(harness)
    assert spec.catalog_factory().options == ()
    assert spec.special_kinds == frozenset()
    # No auth gate: a fail-closed probe that has not been verified would refuse every create.
    assert spec.auth_check is None
@pytest.mark.parametrize(
    ("harness", "expected"),
    [
        (HarnessType.CLAUDE, "claude_process_started"),
        (HarnessType.CODEX, "codex_process_started"),
        (HarnessType.PI_CODING, "pi_process_started"),
    ],
)
def test_process_started_marker_is_declared_on_the_spec(harness: HarnessType, expected: str) -> None:
    """The OOM prioritizer resolves this filename knowing only an agent id, so it must come
    from harness IDENTITY -- available the moment the agent is known -- and not from a live
    tracker instance, which ``_ensure_activity_tracking`` only registers for agents with a
    local state dir. Pinned to the literal mngr touches, since a drift here silently costs
    the prioritizer its aging rather than raising."""
    assert get_harness_spec(harness).process_started_marker_filename == expected


def test_spec_process_started_marker_matches_its_tracker() -> None:
    """The spec field and the tracker ClassVar name the same file for every harness: the
    tracker still uses it to bound transcript staleness, so the two must not drift."""
    for harness in HarnessType:
        spec = get_harness_spec(harness)
        assert spec.process_started_marker_filename == spec.tracker_class.marker_filename

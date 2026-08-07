from typing import Any

import pytest

from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.harnesses.claude.activity import ClaudeActivityTracker
from imbue.system_interface.harnesses.codex.activity import CodexActivityTracker
from imbue.system_interface.harnesses.harness_type import DEFAULT_HARNESS
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.harness_type import parse_harness
from imbue.system_interface.harnesses.registry import build_tracker


@pytest.mark.parametrize(
    "harness, expected_type, expected_marker",
    [
        pytest.param(HarnessType.CLAUDE, ClaudeActivityTracker, "claude_process_started", id="claude"),
        pytest.param(HarnessType.CODEX, CodexActivityTracker, "codex_process_started", id="codex"),
    ],
)
def test_build_tracker(harness: HarnessType, expected_type: type, expected_marker: str) -> None:
    tracker = build_tracker(harness)
    assert isinstance(tracker, expected_type)
    # Each harness reads the marker its own mngr plugin writes -- the pairing
    # that used to be a hardcoded claude filename in AgentManager.
    assert tracker.marker_filename == expected_marker


@pytest.mark.parametrize("agent_type", ["wait", "main", "", None], ids=["wait", "main", "empty", "none"])
def test_parse_harness_narrows_a_non_harness_agent_type(agent_type: str | None) -> None:
    """mngr agent types that are not harnesses keep the claude derivation rather than
    losing the indicator -- and they are narrowed HERE, so every lookup downstream is total."""
    assert parse_harness(agent_type) is DEFAULT_HARNESS
    assert isinstance(build_tracker(parse_harness(agent_type)), ClaudeActivityTracker)


def test_every_harness_has_a_spec() -> None:
    """A new HarnessType member with no spec is a KeyError at build time; catch it here."""
    for harness in HarnessType:
        assert build_tracker(harness) is not None


def test_each_tracker_is_independent() -> None:
    """Trackers are per-agent, so one agent's signals never leak into another's."""
    first = build_tracker(HarnessType.CODEX)
    second = build_tracker(HarnessType.CODEX)
    assert first.observe([{"type": "special", "kind": "turn_started", "timestamp": "2026-07-28T00:00:00Z"}]) is True
    assert first.derive(is_agent_running=True, process_started_at=None) == ActivityState.THINKING
    assert second.derive(is_agent_running=True, process_started_at=None) == ActivityState.IDLE


def test_fresh_tracker_is_idle() -> None:
    for harness in HarnessType:
        tracker = build_tracker(harness)
        assert tracker.derive(is_agent_running=True, process_started_at=None) == ActivityState.IDLE


def test_observe_reports_no_change_on_repeat() -> None:
    """A repeated event list must short-circuit, so streamed lines stay cheap."""
    events: list[dict[str, Any]] = [{"type": "special", "kind": "turn_started", "timestamp": "2026-07-28T00:00:00Z"}]
    for harness in HarnessType:
        tracker = build_tracker(harness)
        # Both harnesses cache the tail event type, so the first observe moves a
        # signal even though only codex reads the turn marker as a latch.
        assert tracker.observe(events) is True, f"{harness} should register the first event"
        assert tracker.observe(events) is False, f"{harness} should short-circuit an unchanged list"


def test_codex_turn_latch_drives_state() -> None:
    tracker = build_tracker(HarnessType.CODEX)
    tracker.observe([{"type": "special", "kind": "turn_started", "timestamp": "2026-07-28T00:00:00Z"}])
    assert tracker.derive(is_agent_running=True, process_started_at=None) == ActivityState.THINKING

    tracker.observe(
        [
            {"type": "special", "kind": "turn_started", "timestamp": "2026-07-28T00:00:00Z"},
            {"type": "special", "kind": "turn_completed", "timestamp": "2026-07-28T00:00:01Z"},
        ]
    )
    assert tracker.derive(is_agent_running=True, process_started_at=None) == ActivityState.IDLE


def test_codex_ignores_the_mngr_lifecycle() -> None:
    """The turn latch is authoritative for codex; mngr's RUNNING flag is not."""
    tracker = build_tracker(HarnessType.CODEX)
    tracker.observe([{"type": "special", "kind": "turn_started", "timestamp": "2026-07-28T00:00:00Z"}])
    assert tracker.derive(is_agent_running=False, process_started_at=None) == ActivityState.THINKING


def test_claude_honors_the_mngr_lifecycle() -> None:
    """A stopped claude agent is IDLE regardless of a mid-turn transcript tail."""
    tracker = build_tracker(HarnessType.CLAUDE)
    tracker.observe([{"type": "user_message", "timestamp": "2026-07-28T00:00:00Z"}])
    assert tracker.derive(is_agent_running=True, process_started_at=None) == ActivityState.THINKING
    assert tracker.derive(is_agent_running=False, process_started_at=None) == ActivityState.IDLE


# antigravity is excluded: it writes no ``*_process_started`` marker and runs no staleness
# rung. A turn abandoned by a dead process reads IDLE through the mngr ``active`` lifecycle
# gate (is_agent_running=False) instead, which ``test_not_running_is_idle`` in
# ``antigravity/activity_state_test`` covers.
@pytest.mark.parametrize("harness", [harness for harness in HarnessType if harness != HarnessType.ANTIGRAVITY])
def test_stale_transcript_tail_reads_idle(harness: HarnessType) -> None:
    """A turn abandoned by a prior process must not pin the indicator.

    The tail predates the current process's marker mtime, so the staleness
    override fires. This is the path that was dead for codex while AgentManager
    stat'd the claude marker for every harness: an absent marker yields
    process_started_at=None, which disables the override entirely.
    """
    tracker = build_tracker(harness)
    tracker.observe(
        [
            {"type": "special", "kind": "turn_started", "timestamp": "2026-07-28T00:00:00Z"},
            {"type": "user_message", "timestamp": "2026-07-28T00:00:00Z"},
        ]
    )
    stale_tail_is_live = tracker.derive(is_agent_running=True, process_started_at=None)
    assert stale_tail_is_live != ActivityState.IDLE

    # Marker touched after the tail -> the turn belongs to a dead process.
    # 2100-01-01, comfortably after the tail above.
    restarted_at = 4102444800.0
    assert tracker.derive(is_agent_running=True, process_started_at=restarted_at) == ActivityState.IDLE


@pytest.mark.parametrize("harness", list(HarnessType))
def test_reset_settles_on_idle(harness: HarnessType) -> None:
    tracker = build_tracker(harness)
    tracker.observe(
        [
            {"type": "special", "kind": "turn_started", "timestamp": "2026-07-28T00:00:00Z"},
            {"type": "user_message", "timestamp": "2026-07-28T00:00:00Z"},
        ]
    )
    assert tracker.derive(is_agent_running=True, process_started_at=None) != ActivityState.IDLE
    tracker.reset()
    assert tracker.derive(is_agent_running=True, process_started_at=None) == ActivityState.IDLE


@pytest.mark.parametrize("harness", list(HarnessType))
def test_pending_tool_use_reads_tool_running(harness: HarnessType) -> None:
    tracker = build_tracker(harness)
    tracker.observe(
        [
            {"type": "special", "kind": "turn_started", "timestamp": "2026-07-28T00:00:00Z"},
            {
                "type": "assistant_message",
                "timestamp": "2026-07-28T00:00:01Z",
                "tool_calls": [{"tool_call_id": "call-1", "tool_name": "Bash"}],
            },
        ]
    )
    assert tracker.derive(is_agent_running=True, process_started_at=None) == ActivityState.TOOL_RUNNING

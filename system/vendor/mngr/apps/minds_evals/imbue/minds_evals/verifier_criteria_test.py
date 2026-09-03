"""Unit tests for the verifier's structural gates and wordiness guard, which read the conversation
from the trial's ATIF trajectory. Both ship as self-contained verifier-container scripts under
templates/tests/ (stdlib + rewardkit, not package modules), so the `gate_checks` and
`wordiness_guard` fixtures load them by file path."""

import json
from pathlib import Path
from types import ModuleType
from typing import Any

from imbue.minds_evals.testing import atif_document


def _write_trajectory(tmp_path: Path, steps: list[dict[str, Any]]) -> Path:
    trajectory_path = tmp_path / "trajectory.json"
    trajectory_path.write_text(json.dumps({**atif_document(), "steps": steps}))
    return trajectory_path


def _workspace_shaped_steps() -> list[dict[str, Any]]:
    """Two turns as the workspace's own document records them: several agent inferences per turn,
    some carrying no message, with a framework-injected system step in between."""
    return [
        {"step_id": 1, "source": "user", "message": "Build it"},
        {"step_id": 2, "source": "agent", "message": ""},
        {"step_id": 3, "source": "agent", "message": "On it: setting things up."},
        {"step_id": 4, "source": "system", "message": "SKILL BODY"},
        {"step_id": 5, "source": "agent", "message": "It's ready, open the preview."},
        {"step_id": 6, "source": "user", "message": "Sounds good."},
        {"step_id": 7, "source": "agent", "message": "All done."},
    ]


def test_gates_take_the_agent_replies_from_the_agent_steps(gate_checks: ModuleType, tmp_path: Path) -> None:
    trajectory_path = _write_trajectory(tmp_path, _workspace_shaped_steps())

    assert gate_checks._agent_replies(trajectory_path) == [
        "On it: setting things up.",
        "It's ready, open the preview.",
        "All done.",
    ]


def test_gates_do_not_count_the_greeting_before_the_first_client_turn_as_a_reply(
    gate_checks: ModuleType, tmp_path: Path
) -> None:
    # The workspace's own document opens with the agent's welcome greeting; a wedged agent that then
    # answers every client turn with the same stub must still fail the engagement gate.
    stub = "Not logged in · Please run /login"
    trajectory_path = _write_trajectory(
        tmp_path,
        [
            {"step_id": 1, "source": "system", "message": "WELCOME SKILL BODY"},
            {"step_id": 2, "source": "agent", "message": "Hi! What shall we build?"},
            {"step_id": 3, "source": "user", "message": "Build it"},
            {"step_id": 4, "source": "agent", "message": stub},
            {"step_id": 5, "source": "user", "message": "Sounds good."},
            {"step_id": 6, "source": "agent", "message": stub},
        ],
    )

    assert gate_checks._agent_replies(trajectory_path) == [stub, stub]


def test_gates_report_a_missing_or_malformed_trajectory_as_unreadable(gate_checks: ModuleType, tmp_path: Path) -> None:
    assert gate_checks._agent_replies(tmp_path / "does-not-exist.json") is None
    (tmp_path / "not-a-document.json").write_text("[1, 2]")
    assert gate_checks._agent_replies(tmp_path / "not-a-document.json") is None


def test_gates_stub_pattern_matches_a_wedged_reply_but_not_a_real_one(gate_checks: ModuleType) -> None:
    assert gate_checks._STUB_REPLY_PATTERN.fullmatch("Not logged in · Please run /login") is not None
    assert gate_checks._STUB_REPLY_PATTERN.fullmatch("I'm logged in now, so let's build it.") is None


def test_wordiness_merges_each_turns_agent_messages(wordiness_guard: ModuleType, tmp_path: Path) -> None:
    # Turn 1's two inferences merge into one count; the system step and the empty inference add
    # nothing; turn 2 is its single message.
    trajectory_path = _write_trajectory(tmp_path, _workspace_shaped_steps())

    assert wordiness_guard._agent_turn_word_counts(trajectory_path) == [10, 2]


def test_wordiness_gives_the_greeting_before_the_first_client_turn_no_turn_of_its_own(
    wordiness_guard: ModuleType, tmp_path: Path
) -> None:
    trajectory_path = _write_trajectory(
        tmp_path,
        [
            {"step_id": 1, "source": "agent", "message": "Hi! What shall we build today?"},
            {"step_id": 2, "source": "user", "message": "Build it"},
            {"step_id": 3, "source": "agent", "message": "On it."},
        ],
    )

    assert wordiness_guard._agent_turn_word_counts(trajectory_path) == [2]


def test_wordiness_on_the_hand_built_shape_counts_the_merged_step_as_is(
    wordiness_guard: ModuleType, tmp_path: Path
) -> None:
    trajectory_path = _write_trajectory(
        tmp_path,
        [
            {"step_id": 1, "source": "user", "message": "Build it"},
            {"step_id": 2, "source": "agent", "message": "On it.\n\nAll done."},
        ],
    )

    assert wordiness_guard._agent_turn_word_counts(trajectory_path) == [4]


def test_wordiness_reports_an_unreadable_trajectory_as_none(wordiness_guard: ModuleType, tmp_path: Path) -> None:
    assert wordiness_guard._agent_turn_word_counts(tmp_path / "does-not-exist.json") is None

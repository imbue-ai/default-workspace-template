"""Unit tests for the verifier's structural gates and message-length guard, which read the conversation
from the trial's ATIF trajectory. Both ship as self-contained verifier-container scripts under
templates/tests/verifier/ (stdlib + rewardkit, not package modules), so the `gate_checks` and
`message_length_guard` fixtures load them by file path."""

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


def test_the_timeline_gate_passes_a_trial_that_declared_no_steps(gate_checks: ModuleType, tmp_path: Path) -> None:
    # No plan is not a failure: the agent is not charged for a case that never warranted one.
    assert gate_checks.is_progress_timeline_read({"rendered_block_count": 0, "is_step_command_run": False})


def test_the_timeline_gate_fails_a_run_whose_steps_were_declared_but_not_read(gate_checks: ModuleType) -> None:
    # The agent ran tk step verbs and the renderer recovered nothing: the parser stopped reading tk's
    # output. Left to the judge this scores 10, because an empty timeline is graded as "no plan".
    assert not gate_checks.is_progress_timeline_read({"rendered_block_count": 0, "is_step_command_run": True})


def test_the_timeline_gate_passes_when_the_steps_were_read(gate_checks: ModuleType) -> None:
    assert gate_checks.is_progress_timeline_read({"rendered_block_count": 3, "is_step_command_run": True})


def test_the_timeline_gate_passes_a_trial_captured_before_the_summary_existed(gate_checks: ModuleType) -> None:
    # An absent summary says nothing about the timeline, which is not the agent's failure.
    assert gate_checks.is_progress_timeline_read(None)


def test_message_lengths_group_each_turns_agent_messages(message_length_guard: ModuleType, tmp_path: Path) -> None:
    # Turn 1's two inferences stay separate; the system step and the empty inference add nothing;
    # turn 2 is its single message. Nothing here marks a turn ending, so none is flagged.
    trajectory_path = _write_trajectory(tmp_path, _workspace_shaped_steps())

    assert message_length_guard._agent_turn_messages(trajectory_path) == (
        [[(5, False), (5, False)], [(2, False)]],
        False,
    )


def test_message_lengths_give_the_greeting_before_the_first_client_turn_no_turn_of_its_own(
    message_length_guard: ModuleType, tmp_path: Path
) -> None:
    trajectory_path = _write_trajectory(
        tmp_path,
        [
            {"step_id": 1, "source": "agent", "message": "Hi! What shall we build today?"},
            {"step_id": 2, "source": "user", "message": "Build it"},
            {"step_id": 3, "source": "agent", "message": "On it."},
        ],
    )

    assert message_length_guard._agent_turn_messages(trajectory_path) == ([[(2, False)]], False)


def test_message_lengths_on_the_hand_built_shape_take_the_merged_step_as_the_turns_answer(
    message_length_guard: ModuleType, tmp_path: Path
) -> None:
    # The hand-built trajectory marks no turn ending, so the merged step is the answer by position
    # and is held to the final-message limit rather than the interim one.
    trajectory_path = _write_trajectory(
        tmp_path,
        [
            {"step_id": 1, "source": "user", "message": "Build it"},
            {"step_id": 2, "source": "agent", "message": " ".join(["word"] * 100)},
        ],
    )

    assert message_length_guard._agent_turn_messages(trajectory_path) == ([[(100, False)]], False)
    assert message_length_guard.is_turn_within_limits([(100, False)], False)


def test_message_lengths_report_an_unreadable_trajectory_as_none(
    message_length_guard: ModuleType, tmp_path: Path
) -> None:
    assert message_length_guard._agent_turn_messages(tmp_path / "does-not-exist.json") is None


def test_message_lengths_read_the_turn_ending_the_workspace_recorded(
    message_length_guard: ModuleType, tmp_path: Path
) -> None:
    trajectory_path = _write_trajectory(
        tmp_path,
        [
            {"step_id": 1, "source": "user", "message": "Build it"},
            {
                "step_id": 2,
                "source": "agent",
                "message": "On it.",
                "extra": {"finish_reason": "tool_use"},
            },
            {
                "step_id": 3,
                "source": "agent",
                "message": "All done, here is how to open it.",
                "extra": {"finish_reason": "end_turn"},
            },
        ],
    )

    assert message_length_guard._agent_turn_messages(trajectory_path) == ([[(2, False), (8, True)]], True)


def test_a_turn_whose_answer_is_long_but_whose_status_lines_are_short_passes(
    message_length_guard: ModuleType,
) -> None:
    turn = [(10, False), (25, False), (290, True)]

    assert message_length_guard.is_turn_within_limits(turn, True)


def test_a_chatty_status_line_fails_its_turn_even_when_the_answer_is_short(
    message_length_guard: ModuleType,
) -> None:
    turn = [(31, False), (40, True)]

    assert not message_length_guard.is_turn_within_limits(turn, True)


def test_an_overlong_answer_fails_its_turn(message_length_guard: ModuleType) -> None:
    assert not message_length_guard.is_turn_within_limits([(12, False), (301, True)], True)


def test_a_turn_cut_short_holds_its_last_status_line_to_the_interim_limit(
    message_length_guard: ModuleType,
) -> None:
    # The trial died before the agent ended its turn, so the last recorded message is a status line,
    # not the turn's answer. Taking the last message as the answer by position would let it run to
    # 300 words.
    assert not message_length_guard.is_turn_within_limits([(12, False), (120, False)], True)


def test_a_trial_cut_short_before_any_turn_ended_still_holds_its_status_lines_to_the_interim_limit(
    message_length_guard: ModuleType, tmp_path: Path
) -> None:
    # A workspace document stamps a finish reason on every agent step; a trial that died mid-turn has
    # only `tool_use` and no terminal one anywhere. Keying the fallback on "did any turn end
    # terminally" calls that document markerless and grades it like the hand-built shape, handing the
    # last status line 300 words -- which is the case the marker exists to catch.
    trajectory_path = tmp_path / "trajectory.json"
    trajectory_path.write_text(
        json.dumps(
            {
                "steps": [
                    {"step_id": 1, "source": "user", "message": "Build it"},
                    {"step_id": 2, "source": "agent", "message": "On it.", "extra": {"finish_reason": "tool_use"}},
                    {
                        "step_id": 3,
                        "source": "agent",
                        "message": "word " * 60,
                        "extra": {"finish_reason": "tool_use"},
                    },
                ]
            }
        )
    )

    turns, does_document_record_endings = message_length_guard._agent_turn_messages(trajectory_path)

    assert does_document_record_endings
    assert not message_length_guard.is_turn_within_limits(turns[0], does_document_record_endings)
    # The same turn in the hand-built shape, which stamps nothing, is one merged answer and passes.
    assert message_length_guard.is_turn_within_limits([(60, False)], False)


def test_a_turn_with_no_messages_scores_nothing(message_length_guard: ModuleType) -> None:
    assert not message_length_guard.is_turn_within_limits([], True)


def test_harness_score_is_one_when_nothing_failed(harness_checks: ModuleType) -> None:
    assert harness_checks.score_for(0, 4) == 1.0


def test_harness_score_halves_at_the_configured_count(harness_checks: ModuleType) -> None:
    assert harness_checks.score_for(4, 4) == 0.5


def test_harness_score_keeps_discriminating_past_the_half_mark_and_never_reaches_zero(
    harness_checks: ModuleType,
) -> None:
    # A linear budget floors every badly-broken run at the same 0; these two runs are not equally
    # broken, and no finite count of signatures proves that nothing worked.
    three, ten = harness_checks.score_for(3, 4), harness_checks.score_for(10, 4)

    assert three > ten > 0.0


def test_a_turn_ended_by_a_stop_sequence_or_an_output_limit_is_still_the_turns_answer(
    message_length_guard: ModuleType, tmp_path: Path
) -> None:
    # A delivery message truncated at max_tokens ended the turn just as surely as one that stopped on
    # end_turn. Reading only end_turn as terminal holds that long answer to the interim limit and fails
    # the turn for being long, which inverts what the criterion is for.
    long_answer = " ".join(["word"] * 250)
    trajectory_path = _write_trajectory(
        tmp_path,
        [
            {"step_id": 1, "source": "user", "message": "Build it"},
            {"step_id": 2, "source": "agent", "message": "On it.", "extra": {"finish_reason": "tool_use"}},
            {"step_id": 3, "source": "agent", "message": long_answer, "extra": {"finish_reason": "max_tokens"}},
        ],
    )

    turns, _records_endings = message_length_guard._agent_turn_messages(trajectory_path)

    assert turns == [[(2, False), (250, True)]]
    assert message_length_guard.is_turn_within_limits(turns[0], True)


def test_the_criterion_reports_the_fraction_of_turns_that_kept_their_limits(
    message_length_guard: ModuleType, tmp_path: Path
) -> None:
    trajectory_path = _write_trajectory(
        tmp_path,
        [
            {"step_id": 1, "source": "user", "message": "Build it"},
            # A 40-word status line: over the interim limit, so this turn fails.
            {
                "step_id": 2,
                "source": "agent",
                "message": " ".join(["word"] * 40),
                "extra": {"finish_reason": "tool_use"},
            },
            {"step_id": 3, "source": "agent", "message": "Done.", "extra": {"finish_reason": "end_turn"}},
            {"step_id": 4, "source": "user", "message": "Sounds good."},
            {"step_id": 5, "source": "agent", "message": "All set.", "extra": {"finish_reason": "end_turn"}},
        ],
    )
    turns, _records_endings = message_length_guard._agent_turn_messages(trajectory_path)
    marked = any(is_ending for turn in turns for _, is_ending in turn)

    scored = [message_length_guard.is_turn_within_limits(turn, marked) for turn in turns]

    assert scored == [False, True]
    assert sum(scored) / len(scored) == 0.5

"""Unit tests for the verifier's structural gates and message-length guard, which read the conversation
from the trial's ATIF trajectory. Both ship as self-contained verifier-container scripts under
templates/tests/verifier/ (stdlib + rewardkit, not package modules), so the `gate_checks` and
`message_length_guard` fixtures load them by file path."""

import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

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


@pytest.mark.parametrize(
    ("interim_reason", "terminal_reason"),
    [
        ("tool_use", "end_turn"),
        ("tool_use", "stop_sequence"),
        ("tool_use", "max_tokens"),
        ("toolUse", "stop"),
        ("toolUse", "length"),
    ],
)
def test_a_turn_ended_on_any_terminal_stop_reason_is_the_turns_answer(
    message_length_guard: ModuleType, tmp_path: Path, interim_reason: str, terminal_reason: str
) -> None:
    # Each harness records the end of a turn in its own vocabulary (Anthropic's end_turn, stop_sequence
    # and max_tokens; pi's stop and length), and a message truncated at the output limit ended the turn
    # as surely as one that stopped on its own. A reason missing from the terminal set holds that long
    # answer to the interim limit and fails the turn for being long, which inverts what the criterion
    # is for.
    long_answer = " ".join(["word"] * 250)
    trajectory_path = _write_trajectory(
        tmp_path,
        [
            {"step_id": 1, "source": "user", "message": "Build it"},
            {"step_id": 2, "source": "agent", "message": "On it.", "extra": {"finish_reason": interim_reason}},
            {"step_id": 3, "source": "agent", "message": long_answer, "extra": {"finish_reason": terminal_reason}},
        ],
    )

    turns, does_document_record_endings = message_length_guard._agent_turn_messages(trajectory_path)

    assert turns == [[(2, False), (250, True)]]
    assert message_length_guard.is_turn_within_limits(turns[0], does_document_record_endings)


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


def test_the_judge_is_told_which_skills_and_how_many_workers_the_case_expected(
    expectations_renderer: ModuleType,
) -> None:
    case = {
        "case_id": "mock-only",
        "expectations": {
            "outcome": "A throwaway mock.",
            "process_checks": [
                {"check_id": "skill_required_build_app", "kind": "required_skill", "skill": "build-app"},
                {
                    "check_id": "skill_forbidden_crystallize_creation",
                    "kind": "forbidden_skill",
                    "skill": "crystallize-creation",
                },
                {"check_id": "worker_launches", "kind": "max_worker_launches", "max_worker_launches": 0},
            ],
        },
    }

    rendered = expectations_renderer.render_expectations(case)

    assert "- The `build-app` skill was to be invoked." in rendered
    assert "- The `crystallize-creation` skill was not to be invoked." in rendered
    assert "- At most 0 background worker(s) were to be launched." in rendered


def test_the_judge_is_told_nothing_about_process_for_a_case_that_asked_for_none(
    expectations_renderer: ModuleType,
) -> None:
    rendered = expectations_renderer.render_expectations(
        {"case_id": "todo-app", "expectations": {"outcome": "A working to-do app."}}
    )

    assert "worker" not in rendered
    assert "skill" not in rendered


def test_the_judge_is_told_how_quickly_the_case_wanted_its_goal_met(
    expectations_renderer: ModuleType,
) -> None:
    case = {
        "case_id": "todo-mock",
        "expectations": {
            "outcome": "A mockup, fast.",
            "timing_checks": [
                {
                    "check_id": "time_to_goal",
                    "fast_seconds": 150.0,
                    "slow_seconds": 600.0,
                    "requires_no_failures": ["app"],
                }
            ],
        },
    }

    rendered = expectations_renderer.render_expectations(case)

    # Anchors travel as floats and are authored as whole seconds, so the judge reads them whole.
    assert "- The client's goal was to be met within 150 second(s), and no later than 600 second(s)" in rendered
    assert "the time only counts if nothing failed in: `app`" in rendered


def test_the_judge_is_told_nothing_about_timing_for_a_case_that_did_not_measure_it(
    expectations_renderer: ModuleType,
) -> None:
    rendered = expectations_renderer.render_expectations(
        {"case_id": "todo-app", "expectations": {"outcome": "A working to-do app."}}
    )

    assert "second(s)" not in rendered


def test_the_timing_curve_gives_full_marks_at_and_under_the_fast_anchor(outcome_checks: ModuleType) -> None:
    assert outcome_checks.timing_score(150.0, 150.0, 600.0) == 1.0
    assert outcome_checks.timing_score(20.0, 150.0, 600.0) == 1.0


def test_the_timing_curve_gives_nothing_at_and_over_the_slow_anchor(outcome_checks: ModuleType) -> None:
    assert outcome_checks.timing_score(600.0, 150.0, 600.0) == 0.0
    assert outcome_checks.timing_score(4000.0, 150.0, 600.0) == 0.0


def test_the_timing_curve_is_linear_in_log_time_between_the_anchors(outcome_checks: ModuleType) -> None:
    # The geometric mean of the two anchors scores exactly half, which is what "linear in log time"
    # buys: the ramp is scale-free, so halving a slow trial's time is worth what halving a fast
    # one's is, and per-case anchors stay comparable across cases.
    assert outcome_checks.timing_score(300.0, 150.0, 600.0) == pytest.approx(0.5)
    assert outcome_checks.timing_score(440.0, 220.0, 880.0) == pytest.approx(0.5)
    # And it is monotone: slower always scores less.
    assert outcome_checks.timing_score(200.0, 150.0, 600.0) > outcome_checks.timing_score(400.0, 150.0, 600.0)


def test_the_timing_curve_scores_zero_for_a_time_that_was_never_measured(outcome_checks: ModuleType) -> None:
    # A client that was never satisfied took unboundedly long. That is the agent's, not the
    # harness's, so it is a legitimate zero rather than a grading failure.
    assert outcome_checks.timing_score(None, 150.0, 600.0) == 0.0


@pytest.mark.parametrize(("fast_seconds", "slow_seconds"), [(0.0, 600.0), (-1.0, 600.0), (600.0, 600.0)])
def test_the_timing_curve_scores_zero_for_anchors_that_define_no_curve(
    outcome_checks: ModuleType, fast_seconds: float, slow_seconds: float
) -> None:
    # A criterion in that file must never raise -- it would abort the whole grade, every dimension
    # with it -- so unusable anchors degrade here and are diagnosed at generation time instead.
    assert outcome_checks.timing_score(200.0, fast_seconds, slow_seconds) == 0.0


def _timing_check(prerequisites: list[str]) -> dict[str, Any]:
    return {
        "check_id": "time_to_goal",
        "fast_seconds": 150.0,
        "slow_seconds": 600.0,
        "requires_no_failures": prerequisites,
    }


def _timing_manifest_entries(seconds: float | None, app_status: str) -> list[dict[str, Any]]:
    return [
        {"entry_id": "app_registered", "check_class": "app", "status": app_status},
        {"entry_id": "time_to_goal", "check_class": "timing", "status": "passed", "value": seconds},
    ]


def test_the_timing_class_scores_the_seconds_the_collector_recorded(outcome_checks: ModuleType) -> None:
    score = outcome_checks.timing_class_score([_timing_check(["app"])], _timing_manifest_entries(300.0, "passed"))

    assert score == pytest.approx(0.5)


def test_a_failed_prerequisite_zeroes_even_the_fastest_time(outcome_checks: ModuleType) -> None:
    # Being fast at something other than what the case commissioned is not what the class measures.
    score = outcome_checks.timing_class_score([_timing_check(["app"])], _timing_manifest_entries(10.0, "failed"))

    assert score == 0.0


def test_a_class_the_case_did_not_name_does_not_zero_the_time(outcome_checks: ModuleType) -> None:
    score = outcome_checks.timing_class_score([_timing_check([])], _timing_manifest_entries(10.0, "failed"))

    assert score == 1.0


def test_an_errored_prerequisite_class_does_not_zero_the_time(outcome_checks: ModuleType) -> None:
    # An error is the harness failing to find out; charging the clock for it would hold a broken
    # instrument against the agent.
    score = outcome_checks.timing_class_score([_timing_check(["app"])], _timing_manifest_entries(10.0, "error"))

    assert score == 1.0


def test_the_timing_class_scores_zero_when_no_entry_carries_a_measurement(outcome_checks: ModuleType) -> None:
    score = outcome_checks.timing_class_score([_timing_check(["app"])], _timing_manifest_entries(None, "passed"))

    assert score == 0.0


def test_the_timing_class_scores_zero_when_its_entry_is_missing_altogether(outcome_checks: ModuleType) -> None:
    score = outcome_checks.timing_class_score(
        [_timing_check(["app"])], [{"entry_id": "app_registered", "check_class": "app", "status": "passed"}]
    )

    assert score == 0.0

"""The contract between the two grade-time files that decide which expectation classes are scored.

``outcome/checks.py`` registers one criterion per class present in the expanded check list, and
``finalize.py`` errors a trial whose declared class produced no determinable evidence. If the two
disagree about which classes exist, a class either scores with no infrastructure-failure backstop or
gets a backstop with nothing to score -- silently, in the verifier container, on a nightly run.

Both take that list from the same ``case.json``, so this also covers what ``finalize.py`` does when
the case file itself cannot be read: a case that commissioned a deliverable and one that never did
must not end up scored the same way.

``checks.py`` imports rewardkit and cannot be imported here (this app depends on neither), so its
table is read out of the source with ``ast``; ``finalize.py`` is stdlib-only and comes from the
``finalize`` fixture, which loads it by path the way the other verifier scripts are loaded.
"""

import ast
import json
from pathlib import Path
from types import ModuleType
from typing import Any
from typing import Final

import pytest

from imbue.minds_evals.data_types import ExpandedExpectations
from imbue.minds_evals.template_loading import TEMPLATES_DIR

_CHECKS_PATH = TEMPLATES_DIR / "outcome" / "checks.py"


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = node.value.value
    return constants


def _criterion_by_class() -> tuple[tuple[str, str, str], ...]:
    """checks.py's registration table, read without importing (it needs rewardkit).

    The table's class names are module constants rather than literals, so the constants are
    resolved first; anything else in there is a shape this test does not know how to read, and it
    says so rather than quietly returning a partial table.
    """
    tree = ast.parse(_CHECKS_PATH.read_text())
    constants = _module_string_constants(tree)
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not any(
            isinstance(target, ast.Name) and target.id == "CRITERION_BY_CLASS" for target in node.targets
        ):
            continue
        assert isinstance(node.value, ast.Tuple), "CRITERION_BY_CLASS is expected to be a tuple of rows"
        rows: list[tuple[str, str, str]] = []
        for row in node.value.elts:
            assert isinstance(row, ast.Tuple), "each CRITERION_BY_CLASS row is expected to be a tuple"
            values = [
                constants[element.id]
                if isinstance(element, ast.Name)
                else element.value
                if isinstance(element, ast.Constant)
                else None
                for element in row.elts
            ]
            assert all(isinstance(value, str) for value in values), "unreadable CRITERION_BY_CLASS row: {}".format(
                values
            )
            rows.append((str(values[0]), str(values[1]), str(values[2])))
        return tuple(rows)
    raise AssertionError("checks.py no longer defines CRITERION_BY_CLASS")


def test_ui_flows_are_registered_as_their_own_scored_criterion() -> None:
    assert ("ui_flows", "ui_flow_checks", "ui_flows_completed") in _criterion_by_class()


def test_finalize_errors_a_trial_only_over_classes_checks_py_actually_scores(finalize: ModuleType) -> None:
    # finalize.py's map is the "this class was unmeasurable, so void the trial" list. Every key on
    # it must be a class checks.py scores, or trials would be destroyed over a class nothing reads.
    scored_keys = {expectation_key for _check_class, expectation_key, _name in _criterion_by_class()}

    assert set(finalize.SCORED_CLASS_BY_EXPECTATION_KEY) <= scored_keys
    assert set(finalize.SCORED_CLASS_BY_EXPECTATION_KEY.values()) <= {
        check_class for check_class, _key, _name in _criterion_by_class()
    }


def test_an_unmeasurable_flow_class_does_not_void_the_whole_trial(finalize: ModuleType) -> None:
    # The other classes come from cheap, reliable probes, so an unmeasurable one means the
    # collection phase itself failed and the trial is worth erroring. Flows run through the
    # box's browser and the forward proxy, which are unavailable for whole classes of ordinary
    # reasons -- no browser, no proxy, a dead tunnel -- and voiding the trial would throw away a
    # perfectly good conversation-quality measurement over one of them. checks.py registers no
    # criterion in that case instead, so the flows cost nothing in either direction.
    assert "ui_flow_checks" not in finalize.SCORED_CLASS_BY_EXPECTATION_KEY
    assert "ui_flows" not in finalize.SCORED_CLASS_BY_EXPECTATION_KEY.values()
    # checks.py still scores the class when there IS something determinable to score.
    assert ("ui_flows", "ui_flow_checks", "ui_flows_completed") in _criterion_by_class()


def test_every_scored_key_is_a_real_field_of_the_expanded_expectations() -> None:
    # The expanded form is what travels into the verifier as case.json; a key that does not exist on
    # it would make its criterion silently unregistered forever.
    scored_keys = {expectation_key for _check_class, expectation_key, _name in _criterion_by_class()}

    assert scored_keys <= set(ExpandedExpectations.model_fields)


def test_finalize_splits_a_flow_case_between_quality_and_outcome(finalize: ModuleType) -> None:
    # Composition is unchanged by flows: the outcome dimension carries half of a gated trial's
    # reward however many classes it happens to contain.
    assert finalize.OUTCOME_SHARE == 0.5


def _grade_case(
    finalize: ModuleType,
    tmp_path: Path,
    case_text: str | None,
    is_gated_open: bool,
    # what the pre-step recorded about the trial's harness, or None for a tree it never wrote to
    harness_record: dict[str, Any] | None,
    gate_criteria: Any = None,
) -> tuple[int, Path]:
    """Run finalize.py over a fake verifier tree that differs only in its case file and its harness.

    Gated open, the tree describes the friendliest possible trial -- gates all passed, the
    conversation finished, quality scored, the harness sound -- so that the outcome is decided by the
    case file alone. Every dimension a real verifier emits is present, since an absent one now scores
    zero and would decide the reward instead of the case file.
    Gated closed, it is a timed-out trial with a failed gate: the tree on which every other
    infrastructure diagnosis is skipped. Returns the exit code and the reward path, which a grading
    failure must have left absent.

    ``gate_criteria`` replaces the gates dimension's criteria, for the tests about a verifier that
    scored no gates at all; it takes any JSON value, since that is what reward-details.json may hold.
    By default the criteria are the one criterion ``is_gated_open`` describes.
    """
    verifier_dir = tmp_path / "logs" / "verifier"
    agent_dir = tmp_path / "logs" / "agent"
    tests_dir = tmp_path / "tests"
    for directory in (verifier_dir, agent_dir, tests_dir):
        directory.mkdir(parents=True)
    reward_path = verifier_dir / "reward.json"
    details_path = verifier_dir / "reward-details.json"
    gate_value, test_state = (1.0, "finished") if is_gated_open else (0.0, "timed_out")
    criteria = [{"value": gate_value}] if gate_criteria is None else gate_criteria
    reward_path.write_text(json.dumps({"gates": gate_value, "quality": 0.8, "harness_quality": 1.0}))
    details_path.write_text(json.dumps({"gates": {"criteria": criteria}}))
    (agent_dir / "state.json").write_text(json.dumps({"test_state": test_state}))
    if case_text is not None:
        (tests_dir / "case.json").write_text(case_text)
    harness_path = agent_dir / "harness.json"
    if harness_record is not None:
        harness_path.write_text(json.dumps(harness_record))

    exit_code = finalize.finalize(
        reward_path=reward_path,
        details_path=details_path,
        state_path=agent_dir / "state.json",
        case_path=tests_dir / "case.json",
        manifest_path=agent_dir / "verification" / "manifest.json",
        harness_path=harness_path,
    )
    return exit_code, reward_path


@pytest.mark.parametrize(
    ("case_text", "expected_fragment"),
    (
        pytest.param(None, "cannot read the case file", id="missing"),
        pytest.param('{"expectations": {"files_checks": [}', "not valid JSON", id="unparseable"),
        pytest.param(json.dumps(["todo-app"]), "not a JSON object", id="not_an_object"),
        pytest.param(
            json.dumps({"id": "todo-app", "expectations": "a to-do app"}),
            "not an object",
            id="expectations_not_an_object",
        ),
    ),
)
def test_a_case_file_that_cannot_be_trusted_errors_the_trial_even_when_everything_else_looks_fine(
    finalize: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case_text: str | None,
    expected_fragment: str,
) -> None:
    # Every read of a broken case file degrades to "this case declared no expectations", so a
    # gated-open, fully-collected trial would otherwise be graded quality-only at full weight --
    # a commissioned deliverable silently dropped out of the score.
    exit_code, reward_path = _grade_case(
        finalize, tmp_path, case_text=case_text, is_gated_open=True, harness_record=None
    )
    reported = capsys.readouterr().err

    assert exit_code == 1
    assert not reward_path.exists(), "a reward file left behind is the fake 0.0 harbor would grade"
    assert "grading infrastructure failure" in reported
    assert expected_fragment in reported


@pytest.mark.parametrize("expectations_entry", ({}, {"expectations": None}))
def test_a_case_that_declares_no_expectations_still_grades_quality_only(
    finalize: ModuleType, tmp_path: Path, expectations_entry: dict[str, Any]
) -> None:
    # Absent or null expectations is the bare case (greeting), not a broken one: it must keep
    # scoring the conversation alone, which is what the diagnosis above must not swallow.
    exit_code, reward_path = _grade_case(
        finalize,
        tmp_path,
        case_text=json.dumps({"id": "greeting", **expectations_entry}),
        is_gated_open=True,
        harness_record=None,
    )

    assert exit_code == 0
    # Quality alone, since there are no expectations, then discounted by the harness's share of it.
    assert json.loads(reward_path.read_text())["reward"] == pytest.approx(
        (1.0 - finalize.HARNESS_SHARE) * 0.8 + finalize.HARNESS_SHARE * 1.0
    )


def test_a_broken_case_file_errors_a_trial_that_would_otherwise_grade_zero(
    finalize: ModuleType, tmp_path: Path
) -> None:
    # The evidence diagnosis is skipped on a gated-closed trial, because partial evidence is
    # expected there. The case file is not evidence of how the trial went -- it is part of the
    # task -- so a failed gate or a timeout must not turn a broken one into a quiet 0.0.
    exit_code, reward_path = _grade_case(finalize, tmp_path, case_text=None, is_gated_open=False, harness_record=None)

    assert exit_code == 1
    assert not reward_path.exists()


def test_a_valid_case_file_lets_a_gated_closed_trial_grade_zero(finalize: ModuleType, tmp_path: Path) -> None:
    # The counterpart that keeps the test above honest: on the same closed tree, a readable case
    # file grades the trial rather than erroring it, so the error really is the case file's.
    exit_code, reward_path = _grade_case(
        finalize, tmp_path, case_text=json.dumps({"id": "greeting"}), is_gated_open=False, harness_record=None
    )

    assert exit_code == 0
    assert json.loads(reward_path.read_text())["reward"] == 0.0


@pytest.mark.parametrize(
    "gate_criteria",
    (
        pytest.param([], id="no_criteria"),
        pytest.param(["not-a-criterion"], id="nothing_readable"),
        pytest.param({"not_timed_out": 1.0}, id="criteria_are_not_a_list"),
    ),
)
def test_a_verifier_that_scored_no_structural_gates_errors_the_trial(
    finalize: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], gate_criteria: Any
) -> None:
    # The gates are programmatic criteria with no judge behind them, so a dimension carrying none of
    # them is the verifier failing to run rather than an agent failing them. The reward composition
    # cannot tell those apart -- both leave the trial gated shut -- so grading it would file the
    # harness's own break as the worst run the agent ever had.
    exit_code, reward_path = _grade_case(
        finalize,
        tmp_path,
        case_text=json.dumps({"id": "greeting"}),
        is_gated_open=True,
        harness_record=None,
        gate_criteria=gate_criteria,
    )
    reported = capsys.readouterr().err

    assert exit_code == 1
    assert not reward_path.exists(), "a reward file left behind is the fake 0.0 harbor would grade"
    assert "scored no structural gates" in reported


def test_the_harness_takes_a_share_of_what_the_trial_earned(finalize: ModuleType) -> None:
    expectations = {"app_checks": ["app_registered"]}

    earned = finalize._earned_reward(
        {"quality": 1.0, "outcome": 1.0, "harness_quality": 0.0}, expectations, is_harness_quality_scored=True
    )

    # Quality and outcome are both perfect, so the whole shortfall is the harness's share of them.
    assert earned == 1.0 - finalize.HARNESS_SHARE


def test_a_broken_harness_costs_a_trial_that_did_everything_else_well(finalize: ModuleType) -> None:
    expectations = {"app_checks": ["app_registered"]}
    rewards = {"quality": 0.8, "outcome": 1.0}

    sound = finalize._earned_reward({**rewards, "harness_quality": 1.0}, expectations, is_harness_quality_scored=True)
    broken = finalize._earned_reward({**rewards, "harness_quality": 0.2}, expectations, is_harness_quality_scored=True)

    # The branch under test is the agent's harness, so tooling it breaks has to move the reward --
    # otherwise an agent whose review gate will not load reads as one that chose to skip review.
    assert sound > broken
    assert round(sound - broken, 6) == round(finalize.HARNESS_SHARE * 0.8, 6)


def test_the_harness_share_leaves_the_quality_outcome_parity_alone(finalize: ModuleType) -> None:
    expectations = {"app_checks": ["app_registered"]}

    quality_only = finalize._earned_reward(
        {"quality": 1.0, "outcome": 0.0, "harness_quality": 1.0}, expectations, is_harness_quality_scored=True
    )
    outcome_only = finalize._earned_reward(
        {"quality": 0.0, "outcome": 1.0, "harness_quality": 1.0}, expectations, is_harness_quality_scored=True
    )

    assert quality_only == outcome_only


def test_an_absent_dimension_scores_zero_whichever_dimension_it_is(finalize: ModuleType) -> None:
    expectations = {"app_checks": ["app_registered"]}

    # These rewards are the ones rewardkit just produced, so a dimension is absent only because it
    # failed to emit -- never because the trial predates it, which is what a regrade re-runs. Reading
    # the harness leniently while reading quality strictly would forgive exactly that failure.
    earned_on_quality_and_outcome = 0.5 * 0.6 + 0.5 * 1.0

    perfect_harness = finalize._earned_reward(
        {"quality": 0.6, "outcome": 1.0, "harness_quality": 1.0}, expectations, is_harness_quality_scored=True
    )
    absent_harness = finalize._earned_reward(
        {"quality": 0.6, "outcome": 1.0}, expectations, is_harness_quality_scored=True
    )
    absent_quality = finalize._earned_reward(
        {"outcome": 1.0, "harness_quality": 1.0}, expectations, is_harness_quality_scored=True
    )

    assert perfect_harness == pytest.approx((1.0 - finalize.HARNESS_SHARE) * earned_on_quality_and_outcome + 0.2)
    assert absent_harness == pytest.approx((1.0 - finalize.HARNESS_SHARE) * earned_on_quality_and_outcome)
    assert absent_quality == pytest.approx((1.0 - finalize.HARNESS_SHARE) * 0.5 + 0.2)


def test_a_trial_on_a_harness_the_dimension_cannot_measure_earns_no_harness_share(finalize: ModuleType) -> None:
    # harness_quality is claude-shaped, so on another harness it is not scored at all. Its share is
    # dropped rather than redistributed, which is what keeps the reward on one range across arms.
    expectations = {"app_checks": ["app_registered"]}
    rewards = {"quality": 0.6, "outcome": 1.0, "harness_quality": 1.0}

    earned = finalize._earned_reward(rewards, expectations, is_harness_quality_scored=False)

    assert earned == pytest.approx(0.5 * 0.6 + 0.5 * 1.0)


def _details_of(tmp_path: Path) -> dict[str, Any]:
    """The reward-details.json a graded tree left behind."""
    return json.loads((tmp_path / "logs" / "verifier" / "reward-details.json").read_text())


# What the harness pre-step left behind, and the harness block finalize.py must compose from it. The
# unreadable shapes are the contract between two scripts that cannot import each other: a key
# renamed on one side would otherwise take the dimension away from every claude trial silently, so
# they read as a scored claude trial exactly like an absent record does.
_HARNESS_RECORD_CASES: Final[tuple[tuple[str, dict[str, Any] | None, dict[str, Any]], ...]] = (
    ("absent", None, {"name": "claude", "is_harness_quality_scored": True}),
    (
        "claude",
        {"name": "claude", "is_harness_quality_scored": True},
        {"name": "claude", "is_harness_quality_scored": True},
    ),
    (
        "pi-coding",
        {"name": "pi-coding", "is_harness_quality_scored": False},
        {"name": "pi-coding", "is_harness_quality_scored": False},
    ),
    ("no name", {"is_harness_quality_scored": False}, {"name": "claude", "is_harness_quality_scored": True}),
    (
        "empty name",
        {"name": "", "is_harness_quality_scored": False},
        {"name": "claude", "is_harness_quality_scored": True},
    ),
    (
        "verdict is not a bool",
        {"name": "pi-coding", "is_harness_quality_scored": "no"},
        {"name": "claude", "is_harness_quality_scored": True},
    ),
)


@pytest.mark.parametrize(
    ("harness_record", "expected_harness"),
    [pytest.param(record, expected, id=name) for name, record, expected in _HARNESS_RECORD_CASES],
)
def test_the_harness_record_decides_whether_the_trial_is_charged_the_harness_share(
    finalize: ModuleType,
    tmp_path: Path,
    harness_record: dict[str, Any] | None,
    expected_harness: dict[str, Any],
) -> None:
    # The tree always carries a harness_quality score of 1.0, even for the pi-coding record a real
    # pi-coding trial could never produce, since the pre-step removes the dimension: an unscored
    # harness must come out at the quality score alone whatever is in that file.
    exit_code, reward_path = _grade_case(
        finalize,
        tmp_path,
        case_text=json.dumps({"id": "greeting"}),
        is_gated_open=True,
        harness_record=harness_record,
    )
    expected_reward = (
        (1.0 - finalize.HARNESS_SHARE) * 0.8 + finalize.HARNESS_SHARE * 1.0
        if expected_harness["is_harness_quality_scored"]
        else 0.8
    )

    assert exit_code == 0
    assert json.loads(reward_path.read_text())["reward"] == pytest.approx(expected_reward)
    assert _details_of(tmp_path)["harness"] == expected_harness


def test_a_pi_coding_trial_that_failed_its_gates_still_grades_zero(finalize: ModuleType, tmp_path: Path) -> None:
    # Dropping the harness share changes what a gated-open trial earned, never the gate itself.
    exit_code, reward_path = _grade_case(
        finalize,
        tmp_path,
        case_text=json.dumps({"id": "greeting"}),
        is_gated_open=False,
        harness_record={"name": "pi-coding", "is_harness_quality_scored": False},
    )

    assert exit_code == 0
    assert json.loads(reward_path.read_text())["reward"] == 0.0
    # The harness is stamped on a gated-closed trial too, so a reader of a zero can still tell a
    # dimension that did not apply from one that failed to emit.
    assert _details_of(tmp_path)["harness"] == {"name": "pi-coding", "is_harness_quality_scored": False}

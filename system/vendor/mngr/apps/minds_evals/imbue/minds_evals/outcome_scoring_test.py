"""The contract between the two grade-time files that decide which expectation classes are scored.

``outcome/checks.py`` registers one criterion per class present in the expanded check list, and
``finalize.py`` errors a trial whose declared class produced no determinable evidence. If the two
disagree about which classes exist, a class either scores with no infrastructure-failure backstop or
gets a backstop with nothing to score -- silently, in the verifier container, on a nightly run.

Both take that list from the same ``case.json``, so this also covers what ``finalize.py`` does when
the case file itself cannot be read: a case that commissioned a deliverable and one that never did
must not end up scored the same way.

``checks.py`` imports rewardkit and cannot be imported here (this app depends on neither), so its
table is read out of the source with ``ast``; ``finalize.py`` is stdlib-only and is loaded by path
like the other verifier scripts.
"""

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from imbue.minds_evals.data_types import ExpandedExpectations
from imbue.minds_evals.template_loading import TEMPLATES_DIR
from imbue.minds_evals.template_loading import load_template_module

_CHECKS_PATH = TEMPLATES_DIR / "outcome" / "checks.py"


def _load_finalize() -> Any:
    return load_template_module("tests/verifier/finalize.py", "minds_evals_finalize")


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


def test_finalize_errors_a_trial_only_over_classes_checks_py_actually_scores() -> None:
    # finalize.py's map is the "this class was unmeasurable, so void the trial" list. Every key on
    # it must be a class checks.py scores, or trials would be destroyed over a class nothing reads.
    finalize = _load_finalize()
    scored_keys = {expectation_key for _check_class, expectation_key, _name in _criterion_by_class()}

    assert set(finalize.SCORED_CLASS_BY_EXPECTATION_KEY) <= scored_keys
    assert set(finalize.SCORED_CLASS_BY_EXPECTATION_KEY.values()) <= {
        check_class for check_class, _key, _name in _criterion_by_class()
    }


def test_an_unmeasurable_flow_class_does_not_void_the_whole_trial() -> None:
    # The other classes come from cheap, reliable probes, so an unmeasurable one means the
    # collection phase itself failed and the trial is worth erroring. Flows run through the
    # box's browser and the forward proxy, which are unavailable for whole classes of ordinary
    # reasons -- no browser, no proxy, a dead tunnel -- and voiding the trial would throw away a
    # perfectly good conversation-quality measurement over one of them. checks.py registers no
    # criterion in that case instead, so the flows cost nothing in either direction.
    finalize = _load_finalize()

    assert "ui_flow_checks" not in finalize.SCORED_CLASS_BY_EXPECTATION_KEY
    assert "ui_flows" not in finalize.SCORED_CLASS_BY_EXPECTATION_KEY.values()
    # checks.py still scores the class when there IS something determinable to score.
    assert ("ui_flows", "ui_flow_checks", "ui_flows_completed") in _criterion_by_class()


def test_every_scored_key_is_a_real_field_of_the_expanded_expectations() -> None:
    # The expanded form is what travels into the verifier as case.json; a key that does not exist on
    # it would make its criterion silently unregistered forever.
    scored_keys = {expectation_key for _check_class, expectation_key, _name in _criterion_by_class()}

    assert scored_keys <= set(ExpandedExpectations.model_fields)


def test_finalize_splits_a_flow_case_between_quality_and_outcome() -> None:
    # Composition is unchanged by flows: the outcome dimension carries half of a gated trial's
    # reward however many classes it happens to contain.
    finalize = _load_finalize()

    assert finalize.OUTCOME_SHARE == 0.5


def _grade_case(tmp_path: Path, case_text: str | None, is_gated_open: bool) -> tuple[int, Path]:
    """Run finalize.py over a fake verifier tree that differs only in its case file.

    Gated open, the tree describes the friendliest possible trial -- gates all passed, the
    conversation finished, quality scored -- so that the outcome is decided by the case file alone.
    Gated closed, it is a timed-out trial with a failed gate: the tree on which every other
    infrastructure diagnosis is skipped. Returns the exit code and the reward path, which a grading
    failure must have left absent.
    """
    verifier_dir = tmp_path / "logs" / "verifier"
    agent_dir = tmp_path / "logs" / "agent"
    tests_dir = tmp_path / "tests"
    for directory in (verifier_dir, agent_dir, tests_dir):
        directory.mkdir(parents=True)
    reward_path = verifier_dir / "reward.json"
    details_path = verifier_dir / "reward-details.json"
    gate_value, test_state = (1.0, "finished") if is_gated_open else (0.0, "timed_out")
    reward_path.write_text(json.dumps({"gates": gate_value, "quality": 0.8}))
    details_path.write_text(json.dumps({"gates": {"criteria": [{"value": gate_value}]}}))
    (agent_dir / "state.json").write_text(json.dumps({"test_state": test_state}))
    if case_text is not None:
        (tests_dir / "case.json").write_text(case_text)

    exit_code = _load_finalize().finalize(
        reward_path=reward_path,
        details_path=details_path,
        state_path=agent_dir / "state.json",
        case_path=tests_dir / "case.json",
        manifest_path=agent_dir / "verification" / "manifest.json",
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
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case_text: str | None, expected_fragment: str
) -> None:
    # Every read of a broken case file degrades to "this case declared no expectations", so a
    # gated-open, fully-collected trial would otherwise be graded quality-only at full weight --
    # a commissioned deliverable silently dropped out of the score.
    exit_code, reward_path = _grade_case(tmp_path, case_text=case_text, is_gated_open=True)
    reported = capsys.readouterr().err

    assert exit_code == 1
    assert not reward_path.exists(), "a reward file left behind is the fake 0.0 harbor would grade"
    assert "grading infrastructure failure" in reported
    assert expected_fragment in reported


@pytest.mark.parametrize("expectations_entry", ({}, {"expectations": None}))
def test_a_case_that_declares_no_expectations_still_grades_quality_only(
    tmp_path: Path, expectations_entry: dict[str, Any]
) -> None:
    # Absent or null expectations is the bare case (greeting), not a broken one: it must keep
    # scoring the conversation alone, which is what the diagnosis above must not swallow.
    exit_code, reward_path = _grade_case(
        tmp_path, case_text=json.dumps({"id": "greeting", **expectations_entry}), is_gated_open=True
    )

    assert exit_code == 0
    assert json.loads(reward_path.read_text())["reward"] == 0.8


def test_a_broken_case_file_errors_a_trial_that_would_otherwise_grade_zero(tmp_path: Path) -> None:
    # The evidence diagnosis is skipped on a gated-closed trial, because partial evidence is
    # expected there. The case file is not evidence of how the trial went -- it is part of the
    # task -- so a failed gate or a timeout must not turn a broken one into a quiet 0.0.
    exit_code, reward_path = _grade_case(tmp_path, case_text=None, is_gated_open=False)

    assert exit_code == 1
    assert not reward_path.exists()


def test_a_valid_case_file_lets_a_gated_closed_trial_grade_zero(tmp_path: Path) -> None:
    # The counterpart that keeps the test above honest: on the same closed tree, a readable case
    # file grades the trial rather than erroring it, so the error really is the case file's.
    exit_code, reward_path = _grade_case(tmp_path, case_text=json.dumps({"id": "greeting"}), is_gated_open=False)

    assert exit_code == 0
    assert json.loads(reward_path.read_text())["reward"] == 0.0

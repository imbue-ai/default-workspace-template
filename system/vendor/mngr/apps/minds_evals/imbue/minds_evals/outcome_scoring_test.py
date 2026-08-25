"""The contract between the two grade-time files that decide which expectation classes are scored.

``outcome/checks.py`` registers one criterion per class present in the lowered check list, and
``finalize.py`` errors a trial whose declared class produced no determinable evidence. If the two
disagree about which classes exist, a class either scores with no infrastructure-failure backstop or
gets a backstop with nothing to score -- silently, in the verifier container, on a nightly run.

``checks.py`` imports rewardkit and cannot be imported here (this app depends on neither), so its
table is read out of the source with ``ast``; ``finalize.py`` is stdlib-only and is loaded by path
like the other verifier scripts.
"""

import ast
import importlib.util
from pathlib import Path
from typing import Any

from imbue.minds_evals.data_types import LoweredExpectations

_TEMPLATES = Path(__file__).parent / "templates"
_CHECKS_PATH = _TEMPLATES / "outcome" / "checks.py"
_FINALIZE_PATH = _TEMPLATES / "tests" / "finalize.py"


def _load_finalize() -> Any:
    spec = importlib.util.spec_from_file_location("minds_evals_finalize", _FINALIZE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    scored_keys = {lowered_key for _check_class, lowered_key, _name in _criterion_by_class()}

    assert set(finalize.SCORED_CLASS_BY_LOWERED_KEY) <= scored_keys
    assert set(finalize.SCORED_CLASS_BY_LOWERED_KEY.values()) <= {
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

    assert "ui_flow_checks" not in finalize.SCORED_CLASS_BY_LOWERED_KEY
    assert "ui_flows" not in finalize.SCORED_CLASS_BY_LOWERED_KEY.values()
    # checks.py still scores the class when there IS something determinable to score.
    assert ("ui_flows", "ui_flow_checks", "ui_flows_completed") in _criterion_by_class()


def test_every_scored_key_is_a_real_field_of_the_lowered_expectations() -> None:
    # The lowered form is what travels into the verifier as case.json; a key that does not exist on
    # it would make its criterion silently unregistered forever.
    scored_keys = {lowered_key for _check_class, lowered_key, _name in _criterion_by_class()}

    assert scored_keys <= set(LoweredExpectations.model_fields)


def test_finalize_splits_a_flow_case_between_quality_and_outcome() -> None:
    # Composition is unchanged by flows: the outcome dimension carries half of a gated trial's
    # reward however many classes it happens to contain.
    finalize = _load_finalize()

    assert finalize.OUTCOME_SHARE == 0.5

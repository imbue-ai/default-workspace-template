from pathlib import Path

import pytest

from imbue.mngr_behaviors.data_types import BehaviorUnitKind
from imbue.mngr_behaviors.testing import write_behavior_corpus
from imbue.mngr_witness.corpus import CorpusInvalidError
from imbue.mngr_witness.corpus import FeatureSlugCollisionError
from imbue.mngr_witness.corpus import NoUnitsSelectedError
from imbue.mngr_witness.corpus import UnitSelection
from imbue.mngr_witness.corpus import discover_feature_tasks
from imbue.mngr_witness.corpus import feature_task_slug
from imbue.mngr_witness.corpus import scan_valid_corpus

_SIGNIN_FEATURE = """Feature: Sign-in

  @fresh-code
  Scenario: Opening a fresh login URL signs the user in
    Given the user is not signed in
    When they open the login URL
    Then the browser lands on "/"
    And the user is signed in

  @spent-code
  Scenario: A spent code is refused
    When a spent code is presented
    Then authentication is refused
"""

_INVARIANTS_FEATURE = """Feature: Authentication invariants

  @single-use-codes
  Rule: A one-time code grants at most one session, ever
    Every presentation of a spent code is refused.
"""

_ROUTING_FEATURE = """Feature: Routing

  @agent-origin
  Scenario: The agent origin routes to the agent
    When a request arrives at the agent origin
    Then it reaches the agent's default service
"""


def _select_everything() -> UnitSelection:
    return UnitSelection(feature_paths=(), area=None, tag=None, unit_kind=None)


@pytest.fixture
def corpus_root(tmp_path: Path) -> Path:
    return write_behavior_corpus(
        tmp_path / "behaviors",
        {
            "authentication/signin.feature": _SIGNIN_FEATURE,
            "authentication/invariants.feature": _INVARIANTS_FEATURE,
            "forwarding/routing.feature": _ROUTING_FEATURE,
        },
    )


def test_feature_tasks_group_units_by_file_with_agent_name_safe_slugs_and_clauses(corpus_root: Path) -> None:
    tasks = discover_feature_tasks(scan_valid_corpus(corpus_root), corpus_root, _select_everything())

    assert [(task.slug, task.coordinates) for task in tasks] == [
        ("authentication-invariants", ("authentication.single-use-codes",)),
        ("authentication-signin", ("authentication.fresh-code", "authentication.spent-code")),
        ("forwarding-routing", ("forwarding.agent-origin",)),
    ]
    signin = next(task for task in tasks if task.slug == "authentication-signin")
    fresh_code = signin.units[0]
    assert fresh_code.clauses == ('Then the browser lands on "/"', "And the user is signed in")
    assert fresh_code.invariants == ("authentication.single-use-codes",)
    assert fresh_code.kind is BehaviorUnitKind.SCENARIO
    invariant = next(task for task in tasks if task.slug == "authentication-invariants").units[0]
    assert invariant.clauses == ("A one-time code grants at most one session, ever",)


def test_selection_by_feature_path_area_tag_and_kind_compose(corpus_root: Path) -> None:
    scan = scan_valid_corpus(corpus_root)

    by_path = discover_feature_tasks(
        scan,
        corpus_root,
        UnitSelection(feature_paths=(Path("forwarding/routing.feature"),), area=None, tag=None, unit_kind=None),
    )
    by_area = discover_feature_tasks(
        scan, corpus_root, UnitSelection(feature_paths=(), area="authentication", tag=None, unit_kind=None)
    )
    by_kind = discover_feature_tasks(
        scan, corpus_root, UnitSelection(feature_paths=(), area=None, tag=None, unit_kind=BehaviorUnitKind.RULE)
    )
    by_tag = discover_feature_tasks(
        scan, corpus_root, UnitSelection(feature_paths=(), area=None, tag="spent-code", unit_kind=None)
    )

    assert [task.slug for task in by_path] == ["forwarding-routing"]
    assert [task.slug for task in by_area] == ["authentication-invariants", "authentication-signin"]
    assert [task.coordinates for task in by_kind] == [("authentication.single-use-codes",)]
    assert [task.coordinates for task in by_tag] == [("authentication.spent-code",)]


def test_selecting_nothing_is_an_error(corpus_root: Path) -> None:
    with pytest.raises(NoUnitsSelectedError):
        discover_feature_tasks(
            scan_valid_corpus(corpus_root),
            corpus_root,
            UnitSelection(feature_paths=(), area="nowhere", tag=None, unit_kind=None),
        )


def test_a_corpus_with_violations_is_refused_before_fan_out(tmp_path: Path) -> None:
    broken_root = write_behavior_corpus(
        tmp_path / "broken", {"area/untagged.feature": "Feature: X\n\n  Scenario: no tag\n    Then it happens\n"}
    )

    with pytest.raises(CorpusInvalidError, match="language violations"):
        scan_valid_corpus(broken_root)


def test_feature_task_slug_qualifies_the_basename_by_its_folders() -> None:
    assert feature_task_slug(Path("browser-authorization/signin.feature")) == "browser-authorization-signin"
    assert feature_task_slug(Path("invariants.feature")) == "invariants"


def test_feature_files_whose_slugs_collide_are_refused(tmp_path: Path) -> None:
    long_stem = "a" * 60
    root = write_behavior_corpus(
        tmp_path / "behaviors",
        {
            f"{long_stem}x.feature": "Feature: X\n\n  @one\n  Scenario: One\n    Then one\n",
            f"{long_stem}y.feature": "Feature: Y\n\n  @two\n  Scenario: Two\n    Then two\n",
        },
    )

    with pytest.raises(FeatureSlugCollisionError):
        discover_feature_tasks(
            scan_valid_corpus(root), root, UnitSelection(feature_paths=(), area=None, tag=None, unit_kind=None)
        )

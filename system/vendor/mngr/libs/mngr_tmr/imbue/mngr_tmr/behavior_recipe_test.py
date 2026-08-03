"""Unit tests for behavior-anchored TMR discovery.

Filter *semantics* (area segment matching, tag-vs-coordinate) are layer 1's,
tested in ``imbue.mngr_behaviors``; here we only cover their threading and
AND-composition, plus the grouping/ordering/naming that is this recipe's own.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr_behaviors.data_types import BehaviorUnitKind
from imbue.mngr_behaviors.testing import write_behavior_corpus
from imbue.mngr_tmr.behavior_recipe import BehaviorCorpusInvalidError
from imbue.mngr_tmr.behavior_recipe import BehaviorMapReduceRecipe
from imbue.mngr_tmr.behavior_recipe import CorpusGateError
from imbue.mngr_tmr.behavior_recipe import NoBehaviorUnitsError
from imbue.mngr_tmr.behavior_recipe import build_behavior_mapper_prompt_for_task
from imbue.mngr_tmr.behavior_recipe import corpus_touching_paths
from imbue.mngr_tmr.behavior_recipe import discover_behavior_tasks

_SIGNIN_FEATURE = """Feature: Sign in
  @fresh-code
  Scenario: Opening a fresh login URL signs the user in
    When the user opens the login URL
    Then the user is signed in

  @used-code
  Scenario: A spent code is refused
    When anyone presents a spent code
    Then authentication is refused
"""

_SESSION_FEATURE = """Feature: Session lifetime
  @survives-restart
  Scenario: Sessions survive a restart
    When the client restarts
    Then the user is still signed in
"""

_BROWSER_AUTH_INVARIANTS_FEATURE = """Feature: Browser-authorization invariants
  @single-use-codes
  Rule: A one-time code grants at most one session, ever
"""

_TUNNELS_FEATURE = """Feature: Tunnels
  @no-tls
  Scenario: Tunnels work without TLS
    When a tunnel is opened
    Then traffic flows
"""


def _write_two_area_corpus(tmp_path: Path) -> Path:
    return write_behavior_corpus(
        tmp_path / "behaviors",
        {
            "browser-authorization/signin.feature": _SIGNIN_FEATURE,
            "browser-authorization/session.feature": _SESSION_FEATURE,
            "browser-authorization/invariants.feature": _BROWSER_AUTH_INVARIANTS_FEATURE,
            "networking/tunnels/hole-punching.feature": _TUNNELS_FEATURE,
        },
    )


def test_discover_behavior_tasks_groups_units_per_feature_file_with_dotted_display_ids(tmp_path: Path) -> None:
    corpus_root = _write_two_area_corpus(tmp_path)

    tasks = discover_behavior_tasks(scan_root=corpus_root, area=None, tag=None, unit_kind=None)

    id_by_display_id = {task.display_id: task.id for task in tasks}
    assert id_by_display_id == {
        "browser-authorization.invariants": "browser-authorization/invariants.feature",
        "browser-authorization.session": "browser-authorization/session.feature",
        "browser-authorization.signin": "browser-authorization/signin.feature",
        "networking.tunnels.hole-punching": "networking/tunnels/hole-punching.feature",
    }


def test_discover_behavior_tasks_preserves_corpus_scan_order(tmp_path: Path) -> None:
    corpus_root = _write_two_area_corpus(tmp_path)

    tasks = discover_behavior_tasks(scan_root=corpus_root, area=None, tag=None, unit_kind=None)

    # scan_corpus walks folders and files in sorted order; grouping must not reorder.
    assert [task.id for task in tasks] == [
        "browser-authorization/invariants.feature",
        "browser-authorization/session.feature",
        "browser-authorization/signin.feature",
        "networking/tunnels/hole-punching.feature",
    ]


def test_discover_behavior_tasks_area_filter_keeps_only_that_subtree(tmp_path: Path) -> None:
    corpus_root = _write_two_area_corpus(tmp_path)

    tasks = discover_behavior_tasks(scan_root=corpus_root, area="networking.tunnels", tag=None, unit_kind=None)

    assert [task.id for task in tasks] == ["networking/tunnels/hole-punching.feature"]


def test_discover_behavior_tasks_tag_filter_selects_single_unit_task(tmp_path: Path) -> None:
    corpus_root = _write_two_area_corpus(tmp_path)

    tasks = discover_behavior_tasks(
        scan_root=corpus_root, area=None, tag="browser-authorization.used-code", unit_kind=None
    )

    assert [task.id for task in tasks] == ["browser-authorization/signin.feature"]


def test_discover_behavior_tasks_unit_kind_filter_keeps_only_rule_files(tmp_path: Path) -> None:
    corpus_root = _write_two_area_corpus(tmp_path)

    tasks = discover_behavior_tasks(scan_root=corpus_root, area=None, tag=None, unit_kind=BehaviorUnitKind.RULE)

    assert [task.id for task in tasks] == ["browser-authorization/invariants.feature"]


def test_discover_behavior_tasks_filters_compose_with_and_semantics(tmp_path: Path) -> None:
    corpus_root = _write_two_area_corpus(tmp_path)

    with pytest.raises(NoBehaviorUnitsError):
        # The area matches units, and the kind matches units, but no unit matches both.
        discover_behavior_tasks(scan_root=corpus_root, area="networking", tag=None, unit_kind=BehaviorUnitKind.RULE)


def test_discover_behavior_tasks_raises_on_corpus_language_violations(tmp_path: Path) -> None:
    corpus_root = write_behavior_corpus(
        tmp_path / "behaviors",
        # An untagged scenario violates the every-unit-carries-an-identity-tag rule.
        {
            "browser-authorization/signin.feature": """Feature: Sign in
  Scenario: Untagged scenario
    When something happens
    Then something is observed
"""
        },
    )

    with pytest.raises(BehaviorCorpusInvalidError) as exc_info:
        discover_behavior_tasks(scan_root=corpus_root, area=None, tag=None, unit_kind=None)

    assert "signin.feature" in str(exc_info.value)


def test_discover_behavior_tasks_raises_when_corpus_has_no_units(tmp_path: Path) -> None:
    corpus_root = write_behavior_corpus(tmp_path / "behaviors", {})

    with pytest.raises(NoBehaviorUnitsError):
        discover_behavior_tasks(scan_root=corpus_root, area=None, tag=None, unit_kind=None)


def test_discover_behavior_tasks_on_live_minds_corpus(tmp_path: Path) -> None:
    """The live corpus discovers into one well-formed task per feature file."""
    live_corpus_root = Path(__file__).resolve().parents[4] / "apps" / "minds" / "behaviors"
    if not live_corpus_root.is_dir():
        pytest.skip("apps/minds behavior corpus not present on this branch")

    tasks = discover_behavior_tasks(scan_root=live_corpus_root, area=None, tag=None, unit_kind=None)

    assert len(tasks) > 0
    assert all(task.id.endswith(".feature") for task in tasks)
    assert all(task.display_id is not None and "/" not in task.display_id for task in tasks)
    # The invariants file fans out as a first-class task like any other.
    assert "browser-authorization/invariants.feature" in {task.id for task in tasks}


def _git(cg: ConcurrencyGroup, cwd: Path, *args: str) -> str:
    result = cg.run_process_to_completion(["git", *args], cwd=cwd)
    return result.stdout


def _write_gate_test_repo(cg: ConcurrencyGroup, repo_dir: Path) -> None:
    """A repo with `main`, plus branches touching only src vs touching the corpus."""
    repo_dir.mkdir()
    _git(cg, repo_dir, "init", "--initial-branch", "main")
    _git(cg, repo_dir, "config", "user.email", "tmr-behaviors-test@example.com")
    _git(cg, repo_dir, "config", "user.name", "tmr-behaviors-test")
    (repo_dir / "behaviors" / "browser-authorization").mkdir(parents=True)
    (repo_dir / "behaviors" / "browser-authorization" / "signin.feature").write_text("Feature: Sign in\n")
    (repo_dir / "src").mkdir()
    (repo_dir / "src" / "app.py").write_text("VALUE = 1\n")
    _git(cg, repo_dir, "add", ".")
    _git(cg, repo_dir, "commit", "-q", "-m", "base")

    _git(cg, repo_dir, "checkout", "-q", "-b", "clean-branch")
    (repo_dir / "src" / "app.py").write_text("VALUE = 2\n")
    _git(cg, repo_dir, "commit", "-q", "-am", "[FIX_IMPL] src change")

    _git(cg, repo_dir, "checkout", "-q", "main")
    _git(cg, repo_dir, "checkout", "-q", "-b", "dirty-branch")
    (repo_dir / "behaviors" / "browser-authorization" / "signin.feature").write_text("Feature: Edited\n")
    (repo_dir / "src" / "app.py").write_text("VALUE = 3\n")
    _git(cg, repo_dir, "commit", "-q", "-am", "mixed change touching the corpus")

    _git(cg, repo_dir, "checkout", "-q", "main")


def test_corpus_touching_paths_is_empty_for_a_branch_outside_the_corpus(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    repo_dir = tmp_path / "repo"
    _write_gate_test_repo(cg, repo_dir)

    touching = corpus_touching_paths(
        source_dir=repo_dir, branch_name="clean-branch", corpus_root=Path("behaviors"), cg=cg
    )

    assert touching == ()


def test_corpus_touching_paths_lists_corpus_files_the_branch_changes(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    repo_dir = tmp_path / "repo"
    _write_gate_test_repo(cg, repo_dir)

    touching = corpus_touching_paths(
        source_dir=repo_dir, branch_name="dirty-branch", corpus_root=Path("behaviors"), cg=cg
    )

    assert touching == ("behaviors/browser-authorization/signin.feature",)


def test_corpus_touching_paths_raises_for_an_unknown_branch(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    repo_dir = tmp_path / "repo"
    _write_gate_test_repo(cg, repo_dir)

    with pytest.raises(CorpusGateError):
        corpus_touching_paths(source_dir=repo_dir, branch_name="no-such-branch", corpus_root=Path("behaviors"), cg=cg)


def test_behavior_recipe_rejects_unsafe_variant_names() -> None:
    with pytest.raises(ValidationError):
        BehaviorMapReduceRecipe(name="bad/name", corpus_root=Path("behaviors"), test_roots=(Path("."),))


def test_behavior_recipe_requires_at_least_one_test_root() -> None:
    with pytest.raises(ValidationError):
        BehaviorMapReduceRecipe(corpus_root=Path("behaviors"), test_roots=())


def test_behavior_recipe_defaults_to_the_tmr_behaviors_variant_name() -> None:
    recipe = BehaviorMapReduceRecipe(corpus_root=Path("apps/minds/behaviors"), test_roots=(Path("apps/minds"),))
    assert recipe.name == "tmr-behaviors"
    assert recipe.mapper_prompt_path is None
    assert recipe.reducer_prompt_path is None


def test_build_behavior_mapper_prompt_for_task_lists_only_that_files_units(tmp_path: Path) -> None:
    corpus_root = _write_two_area_corpus(tmp_path)

    prompt = build_behavior_mapper_prompt_for_task(
        scan_root=corpus_root,
        corpus_root_display=Path("behaviors"),
        task_id="browser-authorization/signin.feature",
        area=None,
        tag=None,
        unit_kind=None,
        test_roots_display=(Path("."),),
        testing_flags=(),
        template_path=None,
    )

    assert "behaviors/browser-authorization/signin.feature" in prompt
    assert "browser-authorization.fresh-code" in prompt
    assert "browser-authorization.used-code" in prompt
    # Units of other files stay out of this task's table...
    assert "browser-authorization.survives-restart" not in prompt
    # ...but the invariants file's Rule appears as in-scope context.
    assert "browser-authorization.single-use-codes" in prompt


def test_build_behavior_mapper_prompt_for_task_respects_filters(tmp_path: Path) -> None:
    corpus_root = _write_two_area_corpus(tmp_path)

    prompt = build_behavior_mapper_prompt_for_task(
        scan_root=corpus_root,
        corpus_root_display=Path("behaviors"),
        task_id="browser-authorization/signin.feature",
        area=None,
        tag="browser-authorization.used-code",
        unit_kind=None,
        test_roots_display=(Path("."),),
        testing_flags=(),
        template_path=None,
    )

    assert "browser-authorization.used-code" in prompt
    assert "browser-authorization.fresh-code" not in prompt

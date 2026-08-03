"""Unit tests for the behavior-anchored TMR prompt builders and templates."""

from pathlib import Path

import pytest

from imbue.mngr_mapreduce.launching import REDUCER_INPUTS_DIRNAME
from imbue.mngr_tmr.behavior_prompts import BehaviorUnitPromptView
from imbue.mngr_tmr.behavior_prompts import MATRIX_ARTIFACT_FILENAME
from imbue.mngr_tmr.behavior_prompts import build_behavior_mapper_prompt
from imbue.mngr_tmr.behavior_prompts import build_behavior_reducer_prompt
from imbue.mngr_tmr.prompts import INTEGRATOR_OUTCOME_FILENAME
from imbue.mngr_tmr.prompts import TESTING_AGENT_OUTCOME_FILENAME


def _sample_units() -> tuple[BehaviorUnitPromptView, ...]:
    return (
        BehaviorUnitPromptView(
            coordinate="browser-authorization.fresh-code",
            kind="scenario",
            name="Opening a fresh login URL signs the user in",
            line=10,
            parent=None,
            invariants=("browser-authorization.single-use-codes", "browser-authorization.no-open-redirects"),
        ),
        BehaviorUnitPromptView(
            coordinate="browser-authorization.single-use-codes",
            kind="rule",
            name="A one-time code grants at most one session, ever",
            line=7,
            parent=None,
            invariants=("browser-authorization.no-open-redirects",),
        ),
    )


def _build_default_mapper_prompt() -> str:
    return build_behavior_mapper_prompt(
        feature_path="apps/minds/behaviors/browser-authorization/signin.feature",
        units=_sample_units(),
        corpus_root="apps/minds/behaviors",
        test_roots=("apps/minds",),
        testing_flags=(),
    )


def test_behavior_mapper_prompt_contains_task_units_and_invariants() -> None:
    prompt = _build_default_mapper_prompt()
    assert "apps/minds/behaviors/browser-authorization/signin.feature" in prompt
    assert "browser-authorization.fresh-code" in prompt
    assert "browser-authorization.single-use-codes" in prompt
    assert "browser-authorization.no-open-redirects" in prompt
    assert TESTING_AGENT_OUTCOME_FILENAME in prompt


def test_behavior_mapper_prompt_declares_the_corpus_read_only() -> None:
    prompt = _build_default_mapper_prompt()
    assert "READ-ONLY" in prompt
    assert "apps/minds/behaviors" in prompt
    # The behavior-seems-wrong exit is the escalation channel, never a corpus edit.
    assert "behavior_problems" in prompt


def test_behavior_mapper_prompt_is_witness_anchored_with_the_verdict_vocabulary() -> None:
    prompt = _build_default_mapper_prompt()
    assert "witnesses" in prompt
    assert "partial=" in prompt
    assert "PARTIAL_STEADY" in prompt
    assert "PARTIAL_IMPROVABLE" in prompt
    assert "untestable in kind" in prompt


def test_behavior_mapper_prompt_includes_testing_flags_in_run_guidance() -> None:
    prompt = build_behavior_mapper_prompt(
        feature_path="apps/minds/behaviors/browser-authorization/signin.feature",
        units=_sample_units(),
        corpus_root="apps/minds/behaviors",
        test_roots=("apps/minds",),
        testing_flags=("-m", "release"),
    )
    assert "-m release" in prompt


def test_behavior_mapper_prompt_renders_default_blocks_and_fixme_channel() -> None:
    prompt = _build_default_mapper_prompt()
    # The generic placement default, overridable via {% block project_guidance %}.
    assert "test taxonomy" in prompt
    assert "FIXME(tmr-behaviors)" in prompt


def test_behavior_mapper_prompt_override_extends_and_fills_blocks(tmp_path: Path) -> None:
    override = tmp_path / "variant_mapper.j2"
    override.write_text(
        '{% extends "behavior_mapper.j2" %}\n'
        "{% block project_guidance %}MINDS PLACEMENT FRAME{% endblock %}\n"
        "{% block infra_blockers %}MINDS INFRA BLOCKERS{% endblock %}\n"
    )
    prompt = build_behavior_mapper_prompt(
        feature_path="apps/minds/behaviors/browser-authorization/signin.feature",
        units=_sample_units(),
        corpus_root="apps/minds/behaviors",
        test_roots=("apps/minds",),
        testing_flags=(),
        template_path=override,
    )
    assert "MINDS PLACEMENT FRAME" in prompt
    assert "MINDS INFRA BLOCKERS" in prompt
    # The contract body is inherited, not duplicated, and the default
    # placement guidance is replaced by the variant's block.
    assert "READ-ONLY" in prompt
    assert "test taxonomy" not in prompt


def test_behavior_reducer_prompt_contains_integrate_and_normalize_contract() -> None:
    prompt = build_behavior_reducer_prompt(
        corpus_root="apps/minds/behaviors",
        test_roots=("apps/minds", "libs/somelib"),
    )
    assert REDUCER_INPUTS_DIRNAME in prompt
    assert INTEGRATOR_OUTCOME_FILENAME in prompt
    assert "should_pull" in prompt
    assert "[CREATE_TEST]" in prompt
    assert "[FIX_IMPL]" in prompt
    # The normalize stage runs matrix over the integrated tree and ships the artifact.
    assert MATRIX_ARTIFACT_FILENAME in prompt
    assert "mngr behaviors matrix --root apps/minds/behaviors --tests apps/minds --tests libs/somelib" in prompt
    # The corpus stays read-only through integration as well.
    assert "apps/minds/behaviors" in prompt


def test_behavior_reducer_prompt_uses_override_template(tmp_path: Path) -> None:
    override = tmp_path / "variant_reducer.j2"
    override.write_text("CUSTOM REDUCER over {{ corpus_root }}\n")
    prompt = build_behavior_reducer_prompt(
        corpus_root="apps/minds/behaviors",
        test_roots=("apps/minds",),
        template_path=override,
    )
    assert prompt.startswith("CUSTOM REDUCER over apps/minds/behaviors")


def test_committed_minds_behaviors_mapper_template_renders() -> None:
    # The minds variant ships apps/minds/tmr/behaviors_mapper.j2 as an {% extends %}
    # of the packaged template; render it through the builder to prove it is
    # valid Jinja, fills both block slots, and inherits the contract body.
    repo_root = Path(__file__).resolve().parents[4]
    minds_template = repo_root / "apps" / "minds" / "tmr" / "behaviors_mapper.j2"
    if not minds_template.is_file():
        pytest.skip("apps/minds tree not present (running outside the monorepo checkout)")
    prompt = build_behavior_mapper_prompt(
        feature_path="apps/minds/behaviors/browser-authorization/signin.feature",
        units=_sample_units(),
        corpus_root="apps/minds/behaviors",
        test_roots=("apps/minds",),
        testing_flags=(),
        template_path=minds_template,
    )
    # The variant's blocks are filled with minds specifics (its placement
    # frame also speaks of the repo taxonomy, so that phrase stays present).
    assert "minds app" in prompt
    assert "minds_snapshot_resume" in prompt
    assert "test taxonomy" in prompt
    assert "@pytest.mark.release" in prompt
    # ...and the packaged contract body is inherited, not duplicated.
    assert "READ-ONLY" in prompt
    assert "PARTIAL_STEADY" in prompt

from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.imbue_common.errors import SwitchError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.mngr_behaviors import witness_collection_plugin
from imbue.mngr_behaviors.data_types import BehaviorUnitKind
from imbue.mngr_mapreduce.bundle import PUBLISH_OUTPUTS_SNIPPET
from imbue.mngr_mapreduce.launching import REDUCER_INPUTS_DIRNAME
from imbue.mngr_witness.corpus import UnitView
from imbue.mngr_witness.outcomes import FindingKind
from imbue.mngr_witness.outcomes import JUNIT_FILENAME
from imbue.mngr_witness.outcomes import MapperOutcome
from imbue.mngr_witness.outcomes import OUTCOME_FILENAME
from imbue.mngr_witness.outcomes import ReduceOutcome
from imbue.mngr_witness.outcomes import ReviewOutcome
from imbue.mngr_witness.outcomes import SCAFFOLDING_GUIDE_FILENAME
from imbue.mngr_witness.outcomes import SetupOutcome
from imbue.mngr_witness.outcomes import UnitVerdict
from imbue.mngr_witness.outcomes import WITNESS_LINKS_FILENAME
from imbue.mngr_witness.outcomes import write_outcome_json
from imbue.mngr_witness.prompts import ANNEALER_JOB_VARIABLES
from imbue.mngr_witness.prompts import ANNEALER_TEMPLATE_NAME
from imbue.mngr_witness.prompts import AnnealerPromptContext
from imbue.mngr_witness.prompts import ExistingWitness
from imbue.mngr_witness.prompts import JUNIT_COMMAND_PREFIX
from imbue.mngr_witness.prompts import MAPPER_JOB_VARIABLES
from imbue.mngr_witness.prompts import MAPPER_TEMPLATE_NAME
from imbue.mngr_witness.prompts import MapperPromptContext
from imbue.mngr_witness.prompts import PROMPT_VARIANT_DIRECTORY_NAME
from imbue.mngr_witness.prompts import PromptVariants
from imbue.mngr_witness.prompts import REVIEWER_JOB_VARIABLES
from imbue.mngr_witness.prompts import REVIEWER_TEMPLATE_NAME
from imbue.mngr_witness.prompts import RUN_PARAMETER_NAMES
from imbue.mngr_witness.prompts import ReviewerPromptContext
from imbue.mngr_witness.prompts import RunFacts
from imbue.mngr_witness.prompts import SCAFFOLD_JOB_VARIABLES
from imbue.mngr_witness.prompts import SCAFFOLD_TEMPLATE_NAME
from imbue.mngr_witness.prompts import ScaffoldPromptContext
from imbue.mngr_witness.prompts import UnintegratedBranch
from imbue.mngr_witness.prompts import WITNESS_COLLECTION_PLUGIN_MODULE
from imbue.mngr_witness.prompts import WITNESS_FIXME_TAG
from imbue.mngr_witness.prompts import WITNESS_LINKS_OUTPUT_PATH_ENV_VAR
from imbue.mngr_witness.prompts import annealer_job_variables
from imbue.mngr_witness.prompts import behaviors_matrix_command
from imbue.mngr_witness.prompts import discover_prompt_variants
from imbue.mngr_witness.prompts import mapper_job_variables
from imbue.mngr_witness.prompts import reviewer_job_variables
from imbue.mngr_witness.prompts import run_parameters
from imbue.mngr_witness.prompts import sample_mapper_outcome
from imbue.mngr_witness.prompts import sample_reduce_outcome
from imbue.mngr_witness.prompts import sample_review_outcome
from imbue.mngr_witness.prompts import sample_setup_outcome
from imbue.mngr_witness.prompts import scaffold_job_variables
from imbue.mngr_witness.prompts import witness_links_harvest_command
from imbue.mngr_witness.testing import render_node_prompt

_CORPUS_ROOT = "libs/mngr_forward/behaviors"
_TEST_ROOT = "libs/mngr_forward"
_FEATURE_PATH = "libs/mngr_forward/behaviors/authentication/signin.feature"
_SETUP_BRANCH = "witness/run-1/setup/scaffold"
_MAPPER_BRANCH = "witness/run-1/map/authentication-signin"
_REVIEW_BRANCH = "witness/run-1/review/authentication-signin"
_INTEGRATION_BRANCH = "witness/run-1/integrate/integration"
_DELIVERABLE_BRANCH = "witness/run-1/reduce/integrated"
_UNINTEGRATED_BRANCH = "witness/run-1/review/forwarding-routing"
_CONFLICTING_PATH = "libs/mngr_forward/conftest.py"
_GENERATION_ROOT = "libs/mngr_forward/imbue/mngr_forward/witnesses"
_GENERATION_MODULE = f"{_GENERATION_ROOT}/authentication/signin_test.py"
_EXISTING_NODE_ID = "libs/mngr_forward/imbue/mngr_forward/auth_test.py::test_otp_single_use"
_EXISTING_PARTIAL = "store-level single-use only, not the HTTP surface"
_GUIDE = (
    "## app_setup\n\n- location: libs/mngr_forward/imbue/mngr_forward/server_test.py\n- use when: any HTTP scenario\n"
)
_DEFAULT_GUIDANCE = "Follow the target project's test taxonomy"
_DEFAULT_INFRA_NOTE = "Nothing is known in advance about what this host cannot run"
_FRESH_CODE_CLAUSES = ('Then the browser lands on the home page "/"', "And the user is signed in")
_RULE_CLAUSE = "A one-time code grants at most one session, ever"


def _facts() -> RunFacts:
    return RunFacts(
        corpus_root=_CORPUS_ROOT,
        test_roots=(_TEST_ROOT,),
        inputs_dirname=REDUCER_INPUTS_DIRNAME,
        publish_snippet=PUBLISH_OUTPUTS_SNIPPET,
        changelog_branch=_DELIVERABLE_BRANCH,
        generation_root=_GENERATION_ROOT,
    )


def _units() -> tuple[UnitView, ...]:
    return (
        UnitView(
            coordinate="authentication.fresh-code",
            kind=BehaviorUnitKind.SCENARIO,
            name="Opening a fresh login URL signs the user in",
            line=10,
            parent=None,
            steps=("Given the user is not signed in", "When the user opens the login URL", *_FRESH_CODE_CLAUSES),
            clauses=_FRESH_CODE_CLAUSES,
            invariants=("single-use-codes",),
        ),
        UnitView(
            coordinate="authentication.single-use-codes",
            kind=BehaviorUnitKind.RULE,
            name=_RULE_CLAUSE,
            line=30,
            parent=None,
            steps=(),
            clauses=(_RULE_CLAUSE,),
            invariants=(),
        ),
    )


def _scaffold_context() -> ScaffoldPromptContext:
    return ScaffoldPromptContext(branch_name=_SETUP_BRANCH, units=_units())


def _mapper_context() -> MapperPromptContext:
    return MapperPromptContext(
        branch_name=_MAPPER_BRANCH,
        feature_path=_FEATURE_PATH,
        units=_units(),
        existing_witnesses=(
            ExistingWitness(
                coordinate="authentication.single-use-codes", node_id=_EXISTING_NODE_ID, partial=_EXISTING_PARTIAL
            ),
        ),
        scaffolding_guide_markdown=_GUIDE,
        generation_module=_GENERATION_MODULE,
    )


def _reviewer_context() -> ReviewerPromptContext:
    return ReviewerPromptContext(
        branch_name=_REVIEW_BRANCH,
        feature_path=_FEATURE_PATH,
        units=_units(),
        mapper_branch=_MAPPER_BRANCH,
        mapper_outcome_json=write_outcome_json(sample_mapper_outcome()),
    )


def _annealer_context() -> AnnealerPromptContext:
    return AnnealerPromptContext(
        branch_name=_DELIVERABLE_BRANCH,
        integrated_branches=(_REVIEW_BRANCH,),
        unintegrated=(UnintegratedBranch(branch_name=_UNINTEGRATED_BRANCH, conflicting_paths=(_CONFLICTING_PATH,)),),
        integration_branch=_INTEGRATION_BRANCH,
        touched_projects=("libs/mngr_forward",),
        changelog_branch=_DELIVERABLE_BRANCH,
        scaffolding_guide_markdown=_GUIDE,
    )


def _render(template_name: str, variant: Path | None) -> str:
    if template_name == SCAFFOLD_TEMPLATE_NAME:
        variables = scaffold_job_variables(_scaffold_context())
    elif template_name == MAPPER_TEMPLATE_NAME:
        variables = mapper_job_variables(_mapper_context())
    elif template_name == REVIEWER_TEMPLATE_NAME:
        variables = reviewer_job_variables(_reviewer_context())
    elif template_name == ANNEALER_TEMPLATE_NAME:
        variables = annealer_job_variables(_annealer_context())
    else:
        raise SwitchError(f"No variables for template {template_name}")
    return render_node_prompt(template_name, variant, _facts(), variables)


def _unwrapped(prompt: str) -> str:
    """The prompt with its line wrapping removed, so prose can be asserted as it reads."""
    return " ".join(prompt.split())


def _assert_states_the_common_contract(prompt: str, branch_name: str) -> None:
    assert "READ-ONLY" in prompt
    assert _CORPUS_ROOT in prompt
    assert f"You are on branch `{branch_name}`" in prompt
    assert "[KIND] area.one, area.two: what changed and why" in prompt
    assert "touch test paths only" in prompt
    assert f"{OUTCOME_FILENAME}" in prompt
    assert f".test_output/{JUNIT_FILENAME}" in prompt
    assert f".test_output/{WITNESS_LINKS_FILENAME}" in prompt
    assert JUNIT_COMMAND_PREFIX in prompt
    assert 'junit_family = "xunit1"' in prompt
    assert witness_links_harvest_command((_TEST_ROOT,)) in prompt
    assert PUBLISH_OUTPUTS_SNIPPET in prompt
    assert "Do not ask the user for input" in prompt


@pytest.mark.parametrize(
    ("declared", "supplied"),
    [
        (SCAFFOLD_JOB_VARIABLES, scaffold_job_variables(_scaffold_context())),
        (MAPPER_JOB_VARIABLES, mapper_job_variables(_mapper_context())),
        (REVIEWER_JOB_VARIABLES, reviewer_job_variables(_reviewer_context())),
        (ANNEALER_JOB_VARIABLES, annealer_job_variables(_annealer_context())),
    ],
)
def test_each_node_binding_supplies_exactly_the_job_variables_the_pipeline_declares(
    declared: tuple[str, ...], supplied: dict[str, str]
) -> None:
    assert set(supplied) == set(declared)


def test_the_run_parameters_are_exactly_the_declared_names() -> None:
    assert set(run_parameters(_facts())) == set(RUN_PARAMETER_NAMES)


def test_scaffold_prompt_states_the_setup_contract() -> None:
    prompt = _render(SCAFFOLD_TEMPLATE_NAME, None)

    _assert_states_the_common_contract(prompt, _SETUP_BRANCH)
    assert "The kinds you may use in this stage are `[SETUP]`." in prompt
    assert "Never write a witnessing test" in prompt
    assert _GENERATION_ROOT in prompt
    assert f".test_output/{SCAFFOLDING_GUIDE_FILENAME}" in prompt
    assert "Never write or change a file under any `changelog/` directory" in prompt
    assert write_outcome_json(sample_setup_outcome()) in prompt
    for unit in _units():
        assert unit.coordinate in prompt
        for step in unit.steps:
            assert step in prompt
    assert "2 units are in scope" in prompt
    assert f"- `{_TEST_ROOT}`\n" in prompt


def test_mapper_prompt_lists_units_with_clauses_invariants_and_existing_witnesses() -> None:
    prompt = _render(MAPPER_TEMPLATE_NAME, None)

    assert f"Your task is the behavior file: {_FEATURE_PATH}" in prompt
    assert "## `authentication.fresh-code`: Opening a fresh login URL signs the user in" in prompt
    assert "A scenario at line 10." in prompt
    assert "A rule at line 30." in prompt
    for clause in (*_FRESH_CODE_CLAUSES, _RULE_CLAUSE):
        assert f"- {clause}\n" in prompt
    assert "In-scope invariants: single-use-codes" in prompt
    assert "In-scope invariants: (none)" in prompt
    assert f"- `{_EXISTING_NODE_ID}` (partial: {_EXISTING_PARTIAL})" in prompt
    assert prompt.count("Existing witnesses:\n\n- (none)") == 1
    assert _GENERATION_MODULE in prompt
    assert _GUIDE in prompt


def test_mapper_prompt_states_the_objective_verdicts_and_gates() -> None:
    prompt = _render(MAPPER_TEMPLATE_NAME, None)

    _assert_states_the_common_contract(prompt, _MAPPER_BRANCH)
    assert "Name the clause." in prompt
    assert "If you cannot name one, do not write it" in prompt
    assert "Match the clause's precision." in prompt
    assert "An unwitnessed unit is not a special mode" in prompt
    assert "Duplicating coverage that already exists is a defect" in _unwrapped(prompt)
    for verdict in UnitVerdict:
        assert f"`{verdict.value}`" in prompt
    assert "`open_world_clauses`" in prompt
    assert (
        "The kinds you may use in this stage are `[CREATE_TEST]`, `[IMPROVE_TEST]`, `[FIX_TEST]`, `[FIX_IMPL]`."
        in prompt
    )
    assert "Never write a changelog entry" in prompt
    assert "`behavior_problems`" in prompt
    assert "`scaffolding_added`" in prompt
    assert f"# {WITNESS_FIXME_TAG} <one-line description" in prompt
    assert "every test named in your trace, by node id, whether you touched it or not" in _unwrapped(prompt)
    assert write_outcome_json(sample_mapper_outcome()) in prompt
    for gate_name in (
        "corpus_untouched",
        "lint",
        "paths",
        "no_changelog",
        "units_reported",
        "links",
        "junit",
        "trace",
    ):
        assert f"`{gate_name}`" in prompt


def test_mapper_prompt_tells_a_unit_without_clauses_to_escalate_instead_of_testing() -> None:
    context = _mapper_context()
    clauseless = UnitView(
        coordinate="authentication.no-claim",
        kind=BehaviorUnitKind.SCENARIO,
        name="A scenario that only acts",
        line=50,
        parent="authentication.single-use-codes",
        steps=("When the user reloads",),
        clauses=(),
        invariants=(),
    )
    variables = mapper_job_variables(
        context.model_copy_update(to_update(context.field_ref().units, (*context.units, clauseless)))
    )

    prompt = render_node_prompt(MAPPER_TEMPLATE_NAME, None, _facts(), variables)

    assert "A scenario at line 50, child of the Rule `authentication.single-use-codes`." in prompt
    assert "- (none: the unit makes no observable claim" in prompt


def test_reviewer_prompt_states_the_adversarial_contract_and_carries_the_mapper_claim() -> None:
    context = _reviewer_context()

    prompt = _render(REVIEWER_TEMPLATE_NAME, None)

    _assert_states_the_common_contract(prompt, _REVIEW_BRANCH)
    assert "You are adversarial" in prompt
    assert f"The mapper worked on branch `{_MAPPER_BRANCH}`" in prompt
    assert f"under `{REDUCER_INPUTS_DIRNAME}/`" in prompt
    assert context.mapper_outcome_json in prompt
    for clause in (*_FRESH_CODE_CLAUSES, _RULE_CLAUSE):
        assert f"- {clause}\n" in prompt
    assert "Existing witnesses:" not in prompt
    assert "The kinds you may use in this stage are `[REVIEW]`, `[FIX_IMPL]`." in prompt
    for kind in FindingKind:
        assert f"`{kind.value}`" in prompt
    assert "`branch_accepted`" in prompt
    assert "gold-plating: remove it" in prompt
    assert "Never write a changelog entry" in prompt
    assert write_outcome_json(sample_review_outcome()) in prompt
    assert "- `trace`: every traced clause is one of the unit's clauses" in prompt


def test_reviewer_prompt_says_when_the_mapper_committed_nothing() -> None:
    context = _reviewer_context()
    variables = reviewer_job_variables(context.model_copy_update(to_update(context.field_ref().mapper_branch, None)))

    prompt = render_node_prompt(REVIEWER_TEMPLATE_NAME, None, _facts(), variables)

    assert "The mapper committed nothing, so none of its work is on a branch of its own" in prompt
    assert _MAPPER_BRANCH not in prompt


def test_annealer_prompt_states_the_integration_normalization_and_changelog_contract() -> None:
    prompt = _render(ANNEALER_TEMPLATE_NAME, None)

    _assert_states_the_common_contract(prompt, _DELIVERABLE_BRANCH)
    assert f"(`{_INTEGRATION_BRANCH}`)" in prompt
    assert f"- `{_REVIEW_BRANCH}`\n" in prompt
    assert f"- `{_UNINTEGRATED_BRANCH}`, conflicting on: {_CONFLICTING_PATH}" in prompt
    assert "git cherry-pick" in prompt
    assert "The kinds you may use in this stage are `[NORMALIZE]`, `[CHANGELOG]`." in prompt
    assert "extract only scaffolding, never a test's core action" in _unwrapped(prompt)
    assert "pytest.param(..., marks=pytest.mark.witnesses(...))" in prompt
    assert f'grep -rn "{WITNESS_FIXME_TAG}"' in prompt
    assert "- `libs/mngr_forward/changelog/witness-run-1-reduce-integrated.md`" in prompt
    assert "`<project>/changelog/witness-run-1-reduce-integrated.md`" in prompt
    assert "`dev` for a path outside both" in prompt
    assert "bullets separated by a blank line" in prompt
    assert _GUIDE in prompt
    assert write_outcome_json(sample_reduce_outcome()) in prompt
    assert "- `changelog`: exactly one entry per touched project" in prompt


@pytest.mark.parametrize(
    ("sample", "model"),
    [
        (sample_setup_outcome(), SetupOutcome),
        (sample_mapper_outcome(), MapperOutcome),
        (sample_review_outcome(), ReviewOutcome),
        (sample_reduce_outcome(), ReduceOutcome),
    ],
)
def test_the_schema_shown_in_each_prompt_round_trips_through_its_model(
    sample: FrozenModel, model: type[FrozenModel]
) -> None:
    assert model.model_validate_json(write_outcome_json(sample)) == sample


@pytest.mark.parametrize(
    "template_name", [SCAFFOLD_TEMPLATE_NAME, MAPPER_TEMPLATE_NAME, REVIEWER_TEMPLATE_NAME, ANNEALER_TEMPLATE_NAME]
)
def test_a_variant_named_like_the_packaged_template_extends_it_and_fills_its_blocks(
    tmp_path: Path, template_name: str
) -> None:
    variant = tmp_path / PROMPT_VARIANT_DIRECTORY_NAME / template_name
    variant.parent.mkdir()
    variant.write_text(
        f'{{% extends "{template_name}" %}}\n'
        "{% block project_guidance %}VARIANT GUIDANCE for {{ corpus_root }}{% endblock %}\n"
        "{% block infra_blockers %}VARIANT INFRA NOTE{% endblock %}\n"
    )

    prompt = _render(template_name, variant)

    assert f"VARIANT GUIDANCE for {_CORPUS_ROOT}" in prompt
    assert "VARIANT INFRA NOTE" in prompt
    assert _DEFAULT_GUIDANCE not in prompt
    assert _DEFAULT_INFRA_NOTE not in prompt
    assert "READ-ONLY" in prompt
    assert PUBLISH_OUTPUTS_SNIPPET in prompt


def test_the_packaged_templates_render_their_default_blocks() -> None:
    for template_name in (
        SCAFFOLD_TEMPLATE_NAME,
        MAPPER_TEMPLATE_NAME,
        REVIEWER_TEMPLATE_NAME,
        ANNEALER_TEMPLATE_NAME,
    ):
        prompt = _render(template_name, None)
        assert "VARIANT" not in prompt
        assert _DEFAULT_GUIDANCE in prompt
        assert _DEFAULT_INFRA_NOTE in prompt
        assert "{%" not in prompt
        assert "{{" not in prompt


def test_discover_prompt_variants_finds_only_the_nodes_a_project_provides(tmp_path: Path) -> None:
    project_dir = tmp_path / "libs" / "proj"
    variant_dir = project_dir / PROMPT_VARIANT_DIRECTORY_NAME
    variant_dir.mkdir(parents=True)
    (variant_dir / MAPPER_TEMPLATE_NAME).write_text('{% extends "mapper.j2" %}')
    (variant_dir / ANNEALER_TEMPLATE_NAME).write_text('{% extends "annealer.j2" %}')
    (variant_dir / SCAFFOLD_TEMPLATE_NAME).mkdir()

    assert discover_prompt_variants(project_dir) == PromptVariants(
        scaffold=None,
        mapper=variant_dir / MAPPER_TEMPLATE_NAME,
        reviewer=None,
        annealer=variant_dir / ANNEALER_TEMPLATE_NAME,
    )
    assert discover_prompt_variants(tmp_path / "elsewhere") == PromptVariants(
        scaffold=None, mapper=None, reviewer=None, annealer=None
    )


def test_the_committed_mngr_forward_variant_fills_project_facts_and_inherits_the_contract(repo_root: Path) -> None:
    variants = discover_prompt_variants(repo_root / "libs" / "mngr_forward")
    assert (
        variants.mapper == repo_root / "libs" / "mngr_forward" / PROMPT_VARIANT_DIRECTORY_NAME / MAPPER_TEMPLATE_NAME
    )

    prompt = _render(MAPPER_TEMPLATE_NAME, variants.mapper)

    assert "starlette.testclient.TestClient" in prompt
    assert "libs/mngr_forward/imbue/mngr_forward/witnesses/" in prompt
    assert "Never use unittest.mock" in prompt
    assert "listeners the test starts on loopback" in prompt
    assert _DEFAULT_GUIDANCE not in prompt
    assert _DEFAULT_INFRA_NOTE not in prompt
    assert "READ-ONLY" in prompt
    assert "Name the clause." in prompt
    assert "`PARTIAL_STEADY`" in prompt
    assert _GENERATION_MODULE in prompt
    assert write_outcome_json(sample_mapper_outcome()) in prompt


def test_the_harvest_command_names_the_real_plugin_and_its_output_variable() -> None:
    assert witness_collection_plugin.__name__ == WITNESS_COLLECTION_PLUGIN_MODULE
    assert WITNESS_LINKS_OUTPUT_PATH_ENV_VAR in Path(witness_collection_plugin.__file__).read_text()
    assert witness_links_harvest_command(("libs/a", "apps/b")) == snapshot(
        "MNGR_BEHAVIORS_WITNESSES_OUTPUT_PATH=.test_output/witness_links.jsonl uv run python -m pytest --rootdir=. --collect-only -q -p imbue.mngr_behaviors.witness_collection_plugin libs/a apps/b"
    )


def test_the_matrix_command_names_every_test_root() -> None:
    assert behaviors_matrix_command("apps/shop/behaviors", ("apps/shop", "libs/cart")) == snapshot(
        "uv run mngr behaviors matrix --root apps/shop/behaviors --tests apps/shop --tests libs/cart"
    )

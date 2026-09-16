"""What every witness agent is told.

The pipeline carries the prompt templates: ``Pipeline.template_by_name`` holds
the packaged family (the four node templates and the partials they include),
and each node's ``prompt_template`` is the packaged template or the project's
variant of it, which extends the packaged one by name. The bindings supply the
values a template reads, all strings: the run-wide ones once, as the
execution's context, and the per-job ones on each job. Sections that list
things (units, existing witnesses, branches) are rendered here from the corpus
and outcome models, so a node template only places them.
"""

from importlib import resources
from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_behaviors.corpus import behavior_unit_kind_record_value
from imbue.mngr_witness.conventions import changelog_entry_path
from imbue.mngr_witness.corpus import UnitView
from imbue.mngr_witness.outcomes import BehaviorProblem
from imbue.mngr_witness.outcomes import Change
from imbue.mngr_witness.outcomes import ChangeStatus
from imbue.mngr_witness.outcomes import Conflict
from imbue.mngr_witness.outcomes import Escalation
from imbue.mngr_witness.outcomes import Finding
from imbue.mngr_witness.outcomes import FindingKind
from imbue.mngr_witness.outcomes import JUNIT_FILENAME
from imbue.mngr_witness.outcomes import MapperOutcome
from imbue.mngr_witness.outcomes import Normalization
from imbue.mngr_witness.outcomes import OUTCOME_FILENAME
from imbue.mngr_witness.outcomes import ReduceOutcome
from imbue.mngr_witness.outcomes import ReviewOutcome
from imbue.mngr_witness.outcomes import SCAFFOLDING_GUIDE_FILENAME
from imbue.mngr_witness.outcomes import ScaffoldingEntry
from imbue.mngr_witness.outcomes import ScaffoldingKind
from imbue.mngr_witness.outcomes import SetupOutcome
from imbue.mngr_witness.outcomes import TraceEntry
from imbue.mngr_witness.outcomes import UnitRecord
from imbue.mngr_witness.outcomes import UnitVerdict
from imbue.mngr_witness.outcomes import WITNESS_LINKS_FILENAME
from imbue.mngr_witness.outcomes import WitnessChangeKind
from imbue.mngr_witness.outcomes import WitnessedTest
from imbue.mngr_witness.outcomes import write_outcome_json

# The packaged node templates; a project variant at <project>/witness/<name> extends one by this name.
SCAFFOLD_TEMPLATE_NAME: Final[str] = "scaffold.j2"
MAPPER_TEMPLATE_NAME: Final[str] = "mapper.j2"
REVIEWER_TEMPLATE_NAME: Final[str] = "reviewer.j2"
ANNEALER_TEMPLATE_NAME: Final[str] = "annealer.j2"
PROMPT_VARIANT_DIRECTORY_NAME: Final[str] = "witness"

# The marker a mapper leaves at a shared-setup blocker and the annealer triages.
WITNESS_FIXME_TAG: Final[str] = "FIXME(witness):"

# Where every node's agent writes its outcome and evidence; the publish snippet packs this directory.
TEST_OUTPUT_DIRNAME: Final[str] = ".test_output"

# Run from the repository root, ``--rootdir=.`` makes junit ``file`` attributes and node ids repository-relative,
# which is how the trace names tests and how the gates look them up in the tree.
JUNIT_COMMAND_PREFIX: Final[str] = f"uv run pytest --rootdir=. --junitxml={TEST_OUTPUT_DIRNAME}/{JUNIT_FILENAME}"

# The collect-only harvest that ``mngr behaviors matrix`` performs, spelled out so an agent can run it itself.
WITNESS_COLLECTION_PLUGIN_MODULE: Final[str] = "imbue.mngr_behaviors.witness_collection_plugin"
WITNESS_LINKS_OUTPUT_PATH_ENV_VAR: Final[str] = "MNGR_BEHAVIORS_WITNESSES_OUTPUT_PATH"

# The names every node template may read from the execution's context; ``run_parameters`` supplies exactly these.
RUN_PARAMETER_NAMES: Final[tuple[str, ...]] = (
    "corpus_root",
    "test_roots_text",
    "test_roots_list",
    "inputs_dirname",
    "publish_snippet",
    "changelog_branch",
    "changelog_entry_basename",
    "generation_root",
    "test_output_dirname",
    "outcome_filename",
    "junit_filename",
    "witness_links_filename",
    "scaffolding_guide_filename",
    "junit_command_prefix",
    "harvest_command",
    "matrix_command",
    "fixme_tag",
)

# The names each node's binding supplies per job; the pipeline declares them as the node's job variables.
COMMON_JOB_VARIABLES: Final[tuple[str, ...]] = ("branch_name", "allowed_kinds_text", "outcome_schema_json")
SCAFFOLD_JOB_VARIABLES: Final[tuple[str, ...]] = (*COMMON_JOB_VARIABLES, "unit_count", "units_list")
MAPPER_JOB_VARIABLES: Final[tuple[str, ...]] = (
    *COMMON_JOB_VARIABLES,
    "feature_path",
    "units_section",
    "scaffolding_guide_markdown",
    "generation_module",
)
REVIEWER_JOB_VARIABLES: Final[tuple[str, ...]] = (
    *COMMON_JOB_VARIABLES,
    "feature_path",
    "units_section",
    "mapper_branch_text",
    "mapper_outcome_json",
    "finding_kinds_text",
)
ANNEALER_JOB_VARIABLES: Final[tuple[str, ...]] = (
    *COMMON_JOB_VARIABLES,
    "integrated_branches_list",
    "unintegrated_list",
    "integration_branch",
    "changelog_paths_list",
    "scaffolding_guide_markdown",
)

_PROMPT_ASSETS_PACKAGE: Final[str] = "imbue.mngr_witness"
_PROMPT_ASSETS_DIRECTORY: Final[str] = "prompt_assets"
_TEMPLATE_SUFFIX: Final[str] = ".j2"
_NONE_BULLET: Final[str] = "- (none)"

_SCAFFOLD_CHANGE_KINDS: Final[tuple[WitnessChangeKind, ...]] = (WitnessChangeKind.SETUP,)
_MAPPER_CHANGE_KINDS: Final[tuple[WitnessChangeKind, ...]] = (
    WitnessChangeKind.CREATE_TEST,
    WitnessChangeKind.IMPROVE_TEST,
    WitnessChangeKind.FIX_TEST,
    WitnessChangeKind.FIX_IMPL,
)
_REVIEWER_CHANGE_KINDS: Final[tuple[WitnessChangeKind, ...]] = (
    WitnessChangeKind.REVIEW,
    WitnessChangeKind.FIX_IMPL,
)
_ANNEALER_CHANGE_KINDS: Final[tuple[WitnessChangeKind, ...]] = (
    WitnessChangeKind.NORMALIZE,
    WitnessChangeKind.CHANGELOG,
)
_MAPPER_CLAUSELESS_NOTE: Final[str] = (
    "- (none: the unit makes no observable claim, so there is nothing to witness;\n"
    "  record a behavior problem for it instead of writing a test)"
)
_REVIEWER_CLAUSELESS_NOTE: Final[str] = (
    "- (none: the unit makes no observable claim; the mapper should have recorded\n"
    "  a behavior problem and written no test)"
)


def _read_packaged_templates() -> dict[str, str]:
    assets = resources.files(_PROMPT_ASSETS_PACKAGE).joinpath(_PROMPT_ASSETS_DIRECTORY)
    return {
        entry.name: entry.read_text(encoding="utf-8")
        for entry in assets.iterdir()
        if entry.name.endswith(_TEMPLATE_SUFFIX)
    }


# The template family every witness pipeline carries: the four node templates and the partials they include.
PACKAGED_TEMPLATE_BY_NAME: Final[dict[str, str]] = _read_packaged_templates()


class RunFacts(FrozenModel):
    """The run-wide facts every node's prompt states; they become the execution's context."""

    corpus_root: str = Field(description="Repo-relative posix path of the corpus root")
    test_roots: tuple[str, ...] = Field(description="Repo-relative posix paths the matrix harvests")
    inputs_dirname: str = Field(description="Where a job's inputs directory lands in the agent's work dir")
    publish_snippet: str = Field(description="The bash every agent runs last to publish its archive")
    changelog_branch: str = Field(description="The name changelog entries must carry; only the annealer writes them")
    generation_root: str = Field(description="Repo-relative directory new witness modules go under")


class ExistingWitness(FrozenModel):
    """A witnesses link already in the tree for one of a mapper's units, from the setup node's harvest."""

    coordinate: str = Field(description="The unit the test witnesses")
    node_id: str = Field(description="The pytest node id of the witnessing test")
    partial: str | None = Field(description="The marker's partial= note, or None when the test covers the unit fully")


class ScaffoldPromptContext(FrozenModel):
    """What one setup job's prompt renders from, beyond the run-wide facts."""

    branch_name: str = Field(description="The branch the agent is on and must publish")
    units: tuple[UnitView, ...] = Field(description="Every selected unit of the corpus")


class MapperPromptContext(FrozenModel):
    """What one mapper's prompt renders from: its feature file, its units, and what the tree already has."""

    branch_name: str = Field(description="The branch the agent is on and must publish")
    feature_path: str = Field(description="Repo-relative posix path of the feature file")
    units: tuple[UnitView, ...] = Field(description="The file's selected units")
    existing_witnesses: tuple[ExistingWitness, ...] = Field(description="Links already in the tree for these units")
    scaffolding_guide_markdown: str = Field(description="The setup node's guide, verbatim")
    generation_module: str = Field(description="Repo-relative path of the module new tests for this file go in")


class ReviewerPromptContext(FrozenModel):
    """What one reviewer's prompt renders from: the mapper's units, branch, and claims."""

    branch_name: str = Field(description="The branch the agent is on and must publish")
    feature_path: str = Field(description="Repo-relative posix path of the feature file")
    units: tuple[UnitView, ...] = Field(description="The file's selected units")
    mapper_branch: str | None = Field(
        description="The mapper branch in the source repo, or None when the mapper committed nothing"
    )
    mapper_outcome_json: str = Field(description="The mapper's outcome.json text, for the reviewer to check")


class UnintegratedBranch(FrozenModel):
    """A reviewed branch whose cherry-pick the orchestrator aborted, for the annealer to integrate by hand."""

    branch_name: str = Field(description="The reviewed branch")
    conflicting_paths: tuple[str, ...] = Field(description="The paths whose cherry-pick conflicted")


class AnnealerPromptContext(FrozenModel):
    """What the annealer's prompt renders from: what is integrated, what is not, and what to write."""

    branch_name: str = Field(description="The branch the agent is on and must publish")
    integrated_branches: tuple[str, ...] = Field(
        description="Already cherry-picked by the orchestrator onto the integration branch"
    )
    unintegrated: tuple[UnintegratedBranch, ...] = Field(description="The annealer must integrate these by hand")
    integration_branch: str = Field(description="The branch the orchestrator built and the annealer finishes")
    touched_projects: tuple[str, ...] = Field(description="Projects the integrated branch touches, one entry each")
    changelog_branch: str = Field(description="The name the changelog entries carry")
    scaffolding_guide_markdown: str = Field(description="The setup node's guide, verbatim")


class PromptVariants(FrozenModel):
    """Per-node template overrides; None means the packaged template."""

    scaffold: Path | None = Field(description="The setup node's variant, or None")
    mapper: Path | None = Field(description="The map node's variant, or None")
    reviewer: Path | None = Field(description="The review node's variant, or None")
    annealer: Path | None = Field(description="The reduce node's variant, or None")


@pure
def no_prompt_variants() -> PromptVariants:
    return PromptVariants(scaffold=None, mapper=None, reviewer=None, annealer=None)


def node_prompt_template(packaged_name: str, variant: Path | None) -> str:
    """The Jinja source a node's jobs render from: the variant file when there is one, else the packaged template.

    A variant extends the packaged template by its bare name, which
    ``PACKAGED_TEMPLATE_BY_NAME`` resolves when the pipeline renders.
    """
    if variant is None:
        return PACKAGED_TEMPLATE_BY_NAME[packaged_name]
    return variant.read_text(encoding="utf-8")


def _variant_if_present(path: Path) -> Path | None:
    return path if path.is_file() else None


def discover_prompt_variants(project_dir: Path) -> PromptVariants:
    """``<project>/witness/<name>.j2`` for each node template that has one; None for the rest."""
    variant_dir = project_dir / PROMPT_VARIANT_DIRECTORY_NAME
    return PromptVariants(
        scaffold=_variant_if_present(variant_dir / SCAFFOLD_TEMPLATE_NAME),
        mapper=_variant_if_present(variant_dir / MAPPER_TEMPLATE_NAME),
        reviewer=_variant_if_present(variant_dir / REVIEWER_TEMPLATE_NAME),
        annealer=_variant_if_present(variant_dir / ANNEALER_TEMPLATE_NAME),
    )


@pure
def witness_links_harvest_command(test_roots: tuple[str, ...]) -> str:
    """The collect-only pass an agent runs from the repository root to ship its witness links."""
    roots = " ".join(test_roots)
    return (
        f"{WITNESS_LINKS_OUTPUT_PATH_ENV_VAR}={TEST_OUTPUT_DIRNAME}/{WITNESS_LINKS_FILENAME} "
        f"uv run python -m pytest --rootdir=. --collect-only -q -p {WITNESS_COLLECTION_PLUGIN_MODULE} {roots}"
    )


@pure
def behaviors_matrix_command(corpus_root: str, test_roots: tuple[str, ...]) -> str:
    tests_arguments = "".join(f" --tests {test_root}" for test_root in test_roots)
    return f"uv run mngr behaviors matrix --root {corpus_root}{tests_arguments}"


@pure
def bullet_list(items: tuple[str, ...], empty: str) -> str:
    """A markdown list with one bullet per item, or the given line when there is nothing to list."""
    return "".join(f"- {item}\n" for item in items) if items else f"{empty}\n"


@pure
def kinds_text(kinds: tuple[WitnessChangeKind, ...]) -> str:
    return ", ".join(f"`[{kind.value}]`" for kind in kinds)


@pure
def _unit_intro(unit: UnitView) -> str:
    parent = f", child of the Rule `{unit.parent}`" if unit.parent else ""
    return f"A {behavior_unit_kind_record_value(unit.kind)} at line {unit.line}{parent}."


@pure
def _steps_block(unit: UnitView) -> str:
    if not unit.steps:
        return ""
    return "\nSteps:\n\n" + "".join(f"    {step}\n" for step in unit.steps)


@pure
def _invariants_text(unit: UnitView) -> str:
    return ", ".join(unit.invariants) if unit.invariants else "(none)"


@pure
def _existing_witnesses_block(unit: UnitView, existing_witnesses: tuple[ExistingWitness, ...]) -> str:
    bullets = tuple(
        f"`{witness.node_id}`" + (f" (partial: {witness.partial})" if witness.partial else "")
        for witness in existing_witnesses
        if witness.coordinate == unit.coordinate
    )
    return "\nExisting witnesses:\n\n" + bullet_list(bullets, _NONE_BULLET)


@pure
def units_section(
    units: tuple[UnitView, ...], existing_witnesses: tuple[ExistingWitness, ...] | None, clauseless_note: str
) -> str:
    """One heading per unit with its steps, clauses, invariants, and, for a mapper, the witnesses the tree already has."""
    sections: list[str] = []
    for unit in units:
        section = (
            f"## `{unit.coordinate}`: {unit.name}\n\n{_unit_intro(unit)}\n{_steps_block(unit)}\nClauses:\n\n"
            + bullet_list(unit.clauses, clauseless_note)
            + f"\nIn-scope invariants: {_invariants_text(unit)}\n"
        )
        if existing_witnesses is not None:
            section += _existing_witnesses_block(unit, existing_witnesses)
        sections.append(section)
    return "\n".join(sections)


@pure
def units_list(units: tuple[UnitView, ...]) -> str:
    """The setup node's compact listing: one bullet per unit with its steps and invariants."""
    entries: list[str] = []
    for unit in units:
        parent = f", child of `{unit.parent}`" if unit.parent else ""
        steps = "".join(f"      {step}\n" for step in unit.steps)
        entries.append(
            f"- `{unit.coordinate}` ({behavior_unit_kind_record_value(unit.kind)}, line {unit.line}{parent}): "
            f"{unit.name}\n{steps}  In-scope invariants: {_invariants_text(unit)}\n"
        )
    return "\n".join(entries)


@pure
def run_parameters(facts: RunFacts) -> dict[str, str]:
    """The execution's context: the run-wide values every node template may read, under ``RUN_PARAMETER_NAMES``."""
    return {
        "corpus_root": facts.corpus_root,
        "test_roots_text": " ".join(facts.test_roots),
        "test_roots_list": bullet_list(tuple(f"`{root}`" for root in facts.test_roots), _NONE_BULLET),
        "inputs_dirname": facts.inputs_dirname,
        "publish_snippet": facts.publish_snippet,
        "changelog_branch": facts.changelog_branch,
        "changelog_entry_basename": f"{facts.changelog_branch.replace('/', '-')}.md",
        "generation_root": facts.generation_root,
        "test_output_dirname": TEST_OUTPUT_DIRNAME,
        "outcome_filename": OUTCOME_FILENAME,
        "junit_filename": JUNIT_FILENAME,
        "witness_links_filename": WITNESS_LINKS_FILENAME,
        "scaffolding_guide_filename": SCAFFOLDING_GUIDE_FILENAME,
        "junit_command_prefix": JUNIT_COMMAND_PREFIX,
        "harvest_command": witness_links_harvest_command(facts.test_roots),
        "matrix_command": behaviors_matrix_command(facts.corpus_root, facts.test_roots),
        "fixme_tag": WITNESS_FIXME_TAG,
    }


@pure
def _common_job_variables(
    branch_name: str, allowed_change_kinds: tuple[WitnessChangeKind, ...], outcome_schema_json: str
) -> dict[str, str]:
    return {
        "branch_name": branch_name,
        "allowed_kinds_text": kinds_text(allowed_change_kinds),
        "outcome_schema_json": outcome_schema_json,
    }


@pure
def scaffold_job_variables(context: ScaffoldPromptContext) -> dict[str, str]:
    return {
        **_common_job_variables(
            context.branch_name, _SCAFFOLD_CHANGE_KINDS, write_outcome_json(sample_setup_outcome())
        ),
        "unit_count": str(len(context.units)),
        "units_list": units_list(context.units),
    }


@pure
def mapper_job_variables(context: MapperPromptContext) -> dict[str, str]:
    return {
        **_common_job_variables(
            context.branch_name, _MAPPER_CHANGE_KINDS, write_outcome_json(sample_mapper_outcome())
        ),
        "feature_path": context.feature_path,
        "units_section": units_section(context.units, context.existing_witnesses, _MAPPER_CLAUSELESS_NOTE),
        "scaffolding_guide_markdown": context.scaffolding_guide_markdown,
        "generation_module": context.generation_module,
    }


@pure
def mapper_branch_text(mapper_branch: str | None) -> str:
    if mapper_branch is None:
        return "The mapper committed nothing, so none of its work is on a branch of its own"
    return f"The mapper worked on branch `{mapper_branch}`"


@pure
def reviewer_job_variables(context: ReviewerPromptContext) -> dict[str, str]:
    return {
        **_common_job_variables(
            context.branch_name, _REVIEWER_CHANGE_KINDS, write_outcome_json(sample_review_outcome())
        ),
        "feature_path": context.feature_path,
        "units_section": units_section(context.units, None, _REVIEWER_CLAUSELESS_NOTE),
        "mapper_branch_text": mapper_branch_text(context.mapper_branch),
        "mapper_outcome_json": context.mapper_outcome_json,
        "finding_kinds_text": ", ".join(f"`{kind.value}`" for kind in FindingKind),
    }


@pure
def annealer_job_variables(context: AnnealerPromptContext) -> dict[str, str]:
    changelog_paths = tuple(
        changelog_entry_path(project, context.changelog_branch) for project in context.touched_projects
    )
    return {
        **_common_job_variables(
            context.branch_name, _ANNEALER_CHANGE_KINDS, write_outcome_json(sample_reduce_outcome())
        ),
        "integrated_branches_list": bullet_list(
            tuple(f"`{branch}`" for branch in context.integrated_branches), _NONE_BULLET
        ),
        "unintegrated_list": bullet_list(
            tuple(
                f"`{entry.branch_name}`, conflicting on: {', '.join(entry.conflicting_paths)}"
                for entry in context.unintegrated
            ),
            _NONE_BULLET,
        ),
        "integration_branch": context.integration_branch,
        "changelog_paths_list": bullet_list(tuple(f"`{path}`" for path in changelog_paths), "- (none yet)"),
        "scaffolding_guide_markdown": context.scaffolding_guide_markdown,
    }


@pure
def sample_setup_outcome() -> SetupOutcome:
    """A realistic SetupOutcome; the scaffold prompt shows its JSON as the schema to write."""
    return SetupOutcome(
        scaffolding=(
            ScaffoldingEntry(
                name="shop_client",
                kind=ScaffoldingKind.FIXTURE,
                location="apps/shop/conftest.py",
                provides="A TestClient over a fresh shop app with an empty cart store",
                use_when="Any scenario that drives the HTTP surface",
            ),
            ScaffoldingEntry(
                name="make_coupon",
                kind=ScaffoldingKind.FACTORY,
                location="apps/shop/imbue/shop/testing.py",
                provides="An unspent coupon registered in the app's coupon store",
                use_when="Scenarios and Rules about coupon redemption",
            ),
        ),
        changes=(
            Change(
                kind=WitnessChangeKind.SETUP,
                status=ChangeStatus.SUCCEEDED,
                commit_hash="0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b",
                summary="Added make_coupon to testing.py; shop_client already existed",
            ),
        ),
        summary_markdown="Found the client fixture, added the coupon factory, created the witnesses package skeleton.",
    )


@pure
def sample_mapper_outcome() -> MapperOutcome:
    """A realistic MapperOutcome covering every verdict shape; the mapper prompt shows its JSON as the schema."""
    return MapperOutcome(
        units=(
            UnitRecord(
                coordinate="checkout.fresh-cart",
                verdict=UnitVerdict.FULL,
                tests=(
                    WitnessedTest(
                        node_id="apps/shop/imbue/shop/witnesses/checkout/cart_test.py::test_fresh_cart_checks_out",
                        partial=None,
                        trace=(
                            TraceEntry(
                                clause="Then the order is confirmed", assertion="assert response.status_code == 201"
                            ),
                            TraceEntry(clause="And the cart is empty", assertion="assert cart.items == ()"),
                        ),
                    ),
                ),
                open_world_clauses=(),
                blockers=(),
                behavior_problems=(),
                summary_markdown="Created one witness; both clauses traced.",
            ),
            UnitRecord(
                coordinate="checkout.single-use-coupons",
                verdict=UnitVerdict.PARTIAL_STEADY,
                tests=(
                    WitnessedTest(
                        node_id="apps/shop/imbue/shop/checkout_test.py::test_spent_coupon_is_refused",
                        partial="does not exercise concurrent redemptions",
                        trace=(
                            TraceEntry(
                                clause="A coupon is redeemed at most once, under any interleaving of requests",
                                assertion="assert second_redemption.status_code == 409",
                            ),
                        ),
                    ),
                ),
                open_world_clauses=("A coupon is redeemed at most once, under any interleaving of requests",),
                blockers=(),
                behavior_problems=(),
                summary_markdown="Adopted the existing witness; the interleaving quantifier is open-world.",
            ),
            UnitRecord(
                coordinate="checkout.gift-receipt",
                verdict=UnitVerdict.NONE,
                tests=(),
                open_world_clauses=(),
                blockers=("Printing a receipt needs the PDF renderer fixture the guide does not name",),
                behavior_problems=(
                    BehaviorProblem(
                        problem="The scenario asserts a printed receipt, but the surface only returns a download link",
                        proposed_edit='Replace "Then a receipt is printed" with "Then a receipt download link is returned"',
                    ),
                ),
                summary_markdown="Left unwitnessed: the clause names an effect the surface does not have.",
            ),
        ),
        changes=(
            Change(
                kind=WitnessChangeKind.CREATE_TEST,
                status=ChangeStatus.SUCCEEDED,
                commit_hash="1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c",
                summary="Witnessed checkout.fresh-cart",
            ),
            Change(
                kind=WitnessChangeKind.FIX_IMPL,
                status=ChangeStatus.FAILED,
                commit_hash=None,
                summary="Could not make the second redemption return 409 without changing the coupon store's lock",
            ),
        ),
        scaffolding_added=(
            ScaffoldingEntry(
                name="empty_cart",
                kind=ScaffoldingKind.FIXTURE,
                location="apps/shop/imbue/shop/witnesses/checkout/conftest.py",
                provides="A cart with no items for the signed-in test user",
                use_when="Checkout scenarios that start from an empty cart",
            ),
        ),
        errored=False,
        summary_markdown="Two of three units witnessed; gift-receipt escalated as a behavior problem.",
    )


@pure
def sample_review_outcome() -> ReviewOutcome:
    """A realistic ReviewOutcome; the reviewer prompt shows its JSON as the schema to write."""
    return ReviewOutcome(
        units=(
            UnitRecord(
                coordinate="checkout.fresh-cart",
                verdict=UnitVerdict.FULL,
                tests=(
                    WitnessedTest(
                        node_id="apps/shop/imbue/shop/witnesses/checkout/cart_test.py::test_fresh_cart_checks_out",
                        partial=None,
                        trace=(
                            TraceEntry(
                                clause="Then the order is confirmed", assertion="assert response.status_code == 201"
                            ),
                            TraceEntry(clause="And the cart is empty", assertion="assert cart.items == ()"),
                        ),
                    ),
                ),
                open_world_clauses=(),
                blockers=(),
                behavior_problems=(),
                summary_markdown="Trace confirmed; removed an assertion on the response body that traced to no clause.",
            ),
            UnitRecord(
                coordinate="checkout.single-use-coupons",
                verdict=UnitVerdict.PARTIAL_IMPROVABLE,
                tests=(
                    WitnessedTest(
                        node_id="apps/shop/imbue/shop/checkout_test.py::test_spent_coupon_is_refused",
                        partial="does not exercise concurrent redemptions",
                        trace=(),
                    ),
                ),
                open_world_clauses=(),
                blockers=(),
                behavior_problems=(),
                summary_markdown="The traced assertion checks the first redemption, not the second; downgraded.",
            ),
        ),
        findings=(
            Finding(
                coordinate="checkout.fresh-cart",
                kind=FindingKind.GOLD_PLATING,
                detail="assert response.json()['currency'] == 'USD' traces to no clause",
                resolved_in_commit="2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d",
            ),
            Finding(
                coordinate="checkout.single-use-coupons",
                kind=FindingKind.MISSING_CLAUSE,
                detail="The traced assertion passes whether or not the coupon was spent",
                resolved_in_commit=None,
            ),
        ),
        branch_accepted=True,
        changes=(
            Change(
                kind=WitnessChangeKind.REVIEW,
                status=ChangeStatus.SUCCEEDED,
                commit_hash="2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d",
                summary="Removed the currency assertion from test_fresh_cart_checks_out",
            ),
        ),
        summary_markdown="One correction landed; one unit downgraded honestly; branch accepted.",
    )


@pure
def sample_reduce_outcome() -> ReduceOutcome:
    """A realistic ReduceOutcome; the annealer prompt shows its JSON as the schema to write."""
    return ReduceOutcome(
        integrated_branches=("witness/run-1/review/checkout-cart", "witness/run-1/review/checkout-coupons"),
        conflicts_resolved=(
            Conflict(
                branch="witness/run-1/review/checkout-coupons",
                commit="3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e",
                paths=("apps/shop/imbue/shop/witnesses/checkout/conftest.py",),
                resolution="Kept both fixtures, then collapsed them in the normalization below",
            ),
        ),
        normalizations=(
            Normalization(
                detail="Collapsed empty_cart (two mappers) into one fixture in apps/shop/conftest.py",
                commit="4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f",
            ),
        ),
        escalations=(
            Escalation(
                detail="checkout.gift-receipt needs a PDF renderer fixture no environment here provides",
                coordinates=("checkout.gift-receipt",),
            ),
        ),
        changelog_paths=("apps/shop/changelog/witness-run-1-reduce-integrated.md",),
        changes=(
            Change(
                kind=WitnessChangeKind.NORMALIZE,
                status=ChangeStatus.SUCCEEDED,
                commit_hash="4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f",
                summary="Collapsed the duplicated empty_cart fixture",
            ),
            Change(
                kind=WitnessChangeKind.CHANGELOG,
                status=ChangeStatus.SUCCEEDED,
                commit_hash="5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a",
                summary="Wrote the apps/shop changelog entry",
            ),
        ),
        summary_markdown="Integrated two branches, resolved one conflict, collapsed one fixture, wrote one changelog entry.",
    )

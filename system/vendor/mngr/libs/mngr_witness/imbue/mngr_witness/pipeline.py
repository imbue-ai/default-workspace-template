"""The witness pipeline as specs/behaviors-mapreduce/spec.md defines it.

An instance of the declarative pipeline model from ``imbue.mngr_mapreduce.pipeline``:
the executor walks it, and the spec's diagram is rendered from it. The prompt
templates are part of the pipeline, so a project's variants make a different
pipeline object: ``make_witness_pipeline`` builds the one a run executes, and
``WITNESS_PIPELINE`` is the packaged default the diagram is drawn from.
"""

from typing import Final

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import UnitFloat
from imbue.mngr_mapreduce.pipeline import AgentWork
from imbue.mngr_mapreduce.pipeline import Artifact
from imbue.mngr_mapreduce.pipeline import DiscoveredFanout
from imbue.mngr_mapreduce.pipeline import Gate
from imbue.mngr_mapreduce.pipeline import Node
from imbue.mngr_mapreduce.pipeline import OrchestratorWork
from imbue.mngr_mapreduce.pipeline import Parameter
from imbue.mngr_mapreduce.pipeline import PerUpstreamJobFanout
from imbue.mngr_mapreduce.pipeline import Pipeline
from imbue.mngr_mapreduce.pipeline import SingleFanout
from imbue.mngr_mapreduce.primitives import ArtifactName
from imbue.mngr_mapreduce.primitives import NodeName
from imbue.mngr_witness.prompts import ANNEALER_JOB_VARIABLES
from imbue.mngr_witness.prompts import ANNEALER_TEMPLATE_NAME
from imbue.mngr_witness.prompts import MAPPER_JOB_VARIABLES
from imbue.mngr_witness.prompts import MAPPER_TEMPLATE_NAME
from imbue.mngr_witness.prompts import PACKAGED_TEMPLATE_BY_NAME
from imbue.mngr_witness.prompts import PromptVariants
from imbue.mngr_witness.prompts import REVIEWER_JOB_VARIABLES
from imbue.mngr_witness.prompts import REVIEWER_TEMPLATE_NAME
from imbue.mngr_witness.prompts import RUN_PARAMETER_NAMES
from imbue.mngr_witness.prompts import SCAFFOLD_JOB_VARIABLES
from imbue.mngr_witness.prompts import SCAFFOLD_TEMPLATE_NAME
from imbue.mngr_witness.prompts import no_prompt_variants
from imbue.mngr_witness.prompts import node_prompt_template

SETUP_NODE_NAME: Final[NodeName] = NodeName("setup")
MAP_NODE_NAME: Final[NodeName] = NodeName("map")
REVIEW_NODE_NAME: Final[NodeName] = NodeName("review")
INTEGRATE_NODE_NAME: Final[NodeName] = NodeName("integrate")
REDUCE_NODE_NAME: Final[NodeName] = NodeName("reduce")

# A mapper or reviewer that fails a gate is left out of the integration and reported as an escalation rather
# than ending the run; below half, the corpus or the scaffolding is the likelier fault, and the run stops.
PARTIAL_HARVEST_REQUIRED_COMPLETION: Final[UnitFloat] = UnitFloat(0.5)

_PARAMETER_DESCRIPTIONS: Final[dict[str, str]] = {
    "corpus_root": "Repo-relative posix path of the corpus root, from --root",
    "test_roots_text": "The test roots the matrix harvests, space-separated, from --tests",
    "test_roots_list": "The same test roots as a markdown list",
    "inputs_dirname": "Where a job's inputs directory lands in the agent's work dir",
    "publish_snippet": "The bash every agent runs last to publish its archive",
    "changelog_branch": "The name changelog entries carry, from --changelog-branch or the deliverable branch",
    "changelog_entry_basename": "The file name of every changelog entry: the changelog branch with slashes as dashes",
    "generation_root": "Where new witness modules go, from --generation-root or the project's witnesses package",
    "test_output_dirname": "The directory at the repository root an agent writes its outcome and evidence to",
    "outcome_filename": "The outcome file's name inside that directory",
    "junit_filename": "The junit report's name inside that directory",
    "witness_links_filename": "The harvested witness links' name inside that directory",
    "scaffolding_guide_filename": "The scaffolding guide's name inside that directory",
    "junit_command_prefix": "How an agent invokes pytest so its report is repository-relative",
    "harvest_command": "The collect-only pass that writes the witness links",
    "matrix_command": "The mngr behaviors matrix command for this corpus and these test roots",
    "fixme_tag": "The marker a mapper leaves at a shared-setup blocker",
}
_PARAMETERS: Final[tuple[Parameter, ...]] = tuple(
    Parameter(name=NonEmptyStr(name), description=_PARAMETER_DESCRIPTIONS[name]) for name in RUN_PARAMETER_NAMES
)

_BASE_COMMIT = Artifact(
    name=ArtifactName("base_commit"),
    description="The operator checkout at the run's base commit B0; every agent is created from its git mirror.",
)
_CORPUS = Artifact(
    name=ArtifactName("corpus"),
    description="The behavior corpus under --root, narrowed by the --feature, --area, --tag, and --unit filters; "
    "read-only for every node.",
)

_SCAFFOLDING_BRANCH = Artifact(
    name=ArtifactName("scaffolding_branch"),
    description="Branch <name>/<execution>/setup/scaffold at the [SETUP] commit S that every mapper starts from; "
    "absent when the tree already has what the corpus needs, and mappers then start from B0.",
)
_SETUP_OUTCOME = Artifact(
    name=ArtifactName("setup_outcome"),
    description="test_output/outcome.json: the SetupOutcome, one entry per fixture or helper found or added, "
    "and the changes made.",
)
_SCAFFOLDING_GUIDE = Artifact(
    name=ArtifactName("scaffolding_guide"),
    description="scaffolding_guide.md: the name, location, purpose, and use-when of every fixture and helper; "
    "injected verbatim into every mapper prompt.",
)
_BASE_WITNESS_LINKS = Artifact(
    name=ArtifactName("base_witness_links"),
    description="witness_links.jsonl from a collect-only pass over the test roots at S: which units are already "
    "witnessed, and by which tests.",
)
_SETUP_JUNIT = Artifact(
    name=ArtifactName("setup_junit"),
    description="junit.xml from the unit suites of every project the scaffolding touched, proving it broke nothing.",
)

_MAPPER_BRANCH = Artifact(
    name=ArtifactName("mapper_branch"),
    description="Branch <name>/<execution>/map/<slug>, M_f: S plus one [KIND] commit per change to the feature "
    "file's witnesses.",
)
_MAPPER_OUTCOME = Artifact(
    name=ArtifactName("mapper_outcome"),
    description="test_output/outcome.json: the MapperOutcome with each unit's verdict and clause trace, the changes, "
    "and any scaffolding added.",
)
_MAPPER_JUNIT = Artifact(
    name=ArtifactName("mapper_junit"),
    description="junit.xml from running every test the trace names, by node id, plus every test module the mapper "
    "touched.",
)
_MAPPER_WITNESS_LINKS = Artifact(
    name=ArtifactName("mapper_witness_links"),
    description="witness_links.jsonl from a collect-only pass over the test roots at M_f.",
)

_REVIEWED_BRANCH = Artifact(
    name=ArtifactName("reviewed_branch"),
    description="Branch <name>/<execution>/review/<slug>, R_f: M_f plus the reviewer's [REVIEW] commits, and "
    "[FIX_IMPL] where a clause exposed an implementation divergence.",
)
_REVIEW_OUTCOME = Artifact(
    name=ArtifactName("review_outcome"),
    description="test_output/outcome.json: the ReviewOutcome, whose verdicts and trace replace the mapper's, with "
    "findings and branch_accepted.",
)
_REVIEW_JUNIT = Artifact(
    name=ArtifactName("review_junit"),
    description="junit.xml from re-running the mapper's traced set after the reviewer's corrections.",
)
_REVIEW_WITNESS_LINKS = Artifact(
    name=ArtifactName("review_witness_links"),
    description="witness_links.jsonl from a collect-only pass over the test roots at R_f.",
)

_INTEGRATION_BRANCH = Artifact(
    name=ArtifactName("integration_branch"),
    description="Branch <name>/<execution>/integrate/integration at S with every accepted R_f cherry-picked in "
    "corpus order, I0; a branch whose cherry-pick conflicted is left out.",
)
_CHERRY_PICK_CONFLICTS = Artifact(
    name=ArtifactName("cherry_pick_conflicts"),
    description="The branch, commit, and conflicting paths of every cherry-pick the orchestrator aborted; the "
    "annealer's unintegrated input.",
)
_INTEGRATED_BRANCH = Artifact(
    name=ArtifactName("integrated_branch"),
    description="Branch <name>/<execution>/reduce/integrated, I: I0 plus the annealer's conflict resolutions, "
    "[NORMALIZE] commits, and [CHANGELOG] commit; the deliverable the operator reviews.",
)
_REDUCE_OUTCOME = Artifact(
    name=ArtifactName("reduce_outcome"),
    description="test_output/outcome.json: the ReduceOutcome listing what was integrated, the conflicts resolved, "
    "the normalizations, the escalations, and the changelog paths.",
)
_REDUCE_JUNIT = Artifact(
    name=ArtifactName("reduce_junit"),
    description="junit.xml from the unit and integration suites of every touched project, plus every traced node id.",
)
_REDUCE_WITNESS_LINKS = Artifact(
    name=ArtifactName("reduce_witness_links"),
    description="witness_links.jsonl from a collect-only pass over the test roots at I.",
)
_CHANGELOG_ENTRIES = Artifact(
    name=ArtifactName("changelog_entries"),
    description="Exactly one <project>/changelog/<changelog_branch>.md per touched project, as the [CHANGELOG] commit.",
)

_REPORT = Artifact(
    name=ArtifactName("report"),
    description="<output_dir>/index.html, rewritten after every state change: per-node tables, the coverage matrix, "
    "reviewer findings, normalizations, and escalations.",
)

_CORPUS_UNTOUCHED_GATE = Gate(
    name=NonEmptyStr("corpus_untouched"),
    description="git diff --name-only <base>..<branch> -- <corpus_root> is empty.",
)
_LINT_GATE = Gate(
    name=NonEmptyStr("lint"),
    description="ruff check and ruff format --check pass on the files the branch touches.",
)
_PATHS_GATE = Gate(
    name=NonEmptyStr("paths"),
    description="Each commit's touched paths satisfy its kind: [SETUP], [CREATE_TEST], [IMPROVE_TEST], [FIX_TEST], "
    "[REVIEW], and [NORMALIZE] touch only test paths; [CHANGELOG] touches only files under changelog/ directories; "
    "[FIX_IMPL] may touch anything outside the corpus; a commit without a recognized kind fails.",
)
_NO_CHANGELOG_GATE = Gate(
    name=NonEmptyStr("no_changelog"),
    description="No file under any changelog/ directory is added or changed.",
)
_CHANGELOG_GATE = Gate(
    name=NonEmptyStr("changelog"),
    description="Exactly one <project>/changelog/<changelog_branch>.md per project the integrated branch touches.",
)
_UNITS_REPORTED_GATE = Gate(
    name=NonEmptyStr("units_reported"),
    description="The outcome's set of coordinates equals the set assigned to the job.",
)
_LINKS_GATE = Gate(
    name=NonEmptyStr("links"),
    description="witness_links.jsonl joins against the corpus with no broken links, and every unit with a verdict "
    "other than NONE has at least one link.",
)
_JUNIT_GATE = Gate(
    name=NonEmptyStr("junit"),
    description="The junit file exists, has no failures or errors, and every node id named in the trace is present "
    "and passed.",
)
_TRACE_GATE = Gate(
    name=NonEmptyStr("trace"),
    description="Every clause named in the trace belongs to its unit; every assertion string occurs in the named "
    "test's source at the branch tip; a FULL unit traces every clause and lists no open-world clause; a "
    "PARTIAL_STEADY unit traces every clause it does not list as open-world.",
)


def _job_variables(names: tuple[str, ...]) -> tuple[NonEmptyStr, ...]:
    return tuple(NonEmptyStr(name) for name in names)


def make_witness_pipeline(variants: PromptVariants) -> Pipeline:
    """The pipeline a run executes: the packaged template family, with each node's template swapped for the project's variant."""
    setup = Node(
        name=SETUP_NODE_NAME,
        summary="One agent from the base commit adds the fixtures and helpers the corpus still lacks as one [SETUP] "
        "commit, writes the scaffolding guide, and harvests the base witness links; it never writes a witnessing "
        "test, and a failure here stops the run before any mapper launches.",
        needs=(_BASE_COMMIT.name, _CORPUS.name),
        produces=(_SCAFFOLDING_BRANCH, _SETUP_OUTCOME, _SCAFFOLDING_GUIDE, _BASE_WITNESS_LINKS, _SETUP_JUNIT),
        work=AgentWork(
            fanout=SingleFanout(description="one agent"),
            prompt_template=NonEmptyStr(node_prompt_template(SCAFFOLD_TEMPLATE_NAME, variants.scaffold)),
            job_variables=_job_variables(SCAFFOLD_JOB_VARIABLES),
            gates=(_CORPUS_UNTOUCHED_GATE, _LINT_GATE, _PATHS_GATE, _LINKS_GATE),
        ),
    )
    map_node = Node(
        name=MAP_NODE_NAME,
        summary="One agent per selected feature file, from the scaffolding commit, witnesses the file's units clause "
        "by clause with the fixtures the guide names and adopts existing witnesses where they are; a branch that "
        "fails a gate is neither reviewed nor integrated.",
        needs=(_SCAFFOLDING_BRANCH.name, _SCAFFOLDING_GUIDE.name, _BASE_WITNESS_LINKS.name, _CORPUS.name),
        produces=(_MAPPER_BRANCH, _MAPPER_OUTCOME, _MAPPER_JUNIT, _MAPPER_WITNESS_LINKS),
        work=AgentWork(
            fanout=DiscoveredFanout(description="one agent per feature file"),
            prompt_template=NonEmptyStr(node_prompt_template(MAPPER_TEMPLATE_NAME, variants.mapper)),
            job_variables=_job_variables(MAPPER_JOB_VARIABLES),
            gates=(
                _CORPUS_UNTOUCHED_GATE,
                _LINT_GATE,
                _PATHS_GATE,
                _NO_CHANGELOG_GATE,
                _UNITS_REPORTED_GATE,
                _LINKS_GATE,
                _JUNIT_GATE,
            ),
            required_completion=PARTIAL_HARVEST_REQUIRED_COMPLETION,
            min_job_count=NonNegativeInt(1),
        ),
    )
    review = Node(
        name=REVIEW_NODE_NAME,
        summary="One adversarial agent per successful mapper, from the mapper's tip, checks every unit's trace, "
        "honesty, duplication, and scope, fixes what it can as [REVIEW] commits, and writes the verdicts that "
        "replace the mapper's from here on.",
        needs=(_MAPPER_BRANCH.name, _MAPPER_OUTCOME.name, _MAPPER_JUNIT.name, _MAPPER_WITNESS_LINKS.name),
        produces=(_REVIEWED_BRANCH, _REVIEW_OUTCOME, _REVIEW_JUNIT, _REVIEW_WITNESS_LINKS),
        work=AgentWork(
            fanout=PerUpstreamJobFanout(over=_MAPPER_BRANCH.name, description="one agent per successful mapper"),
            prompt_template=NonEmptyStr(node_prompt_template(REVIEWER_TEMPLATE_NAME, variants.reviewer)),
            job_variables=_job_variables(REVIEWER_JOB_VARIABLES),
            gates=(
                _CORPUS_UNTOUCHED_GATE,
                _LINT_GATE,
                _PATHS_GATE,
                _NO_CHANGELOG_GATE,
                _UNITS_REPORTED_GATE,
                _LINKS_GATE,
                _JUNIT_GATE,
                _TRACE_GATE,
            ),
            required_completion=PARTIAL_HARVEST_REQUIRED_COMPLETION,
        ),
    )
    integrate = Node(
        name=INTEGRATE_NODE_NAME,
        summary="The orchestrator cherry-picks every accepted reviewed branch onto the scaffolding commit in corpus "
        "order, recording the branch, commit, and paths of every cherry-pick that conflicted.",
        needs=(_REVIEWED_BRANCH.name, _REVIEW_OUTCOME.name, _SCAFFOLDING_BRANCH.name),
        produces=(_INTEGRATION_BRANCH, _CHERRY_PICK_CONFLICTS),
        work=OrchestratorWork(),
    )
    reduce = Node(
        name=REDUCE_NODE_NAME,
        summary="One annealer agent from the integration branch integrates what conflicted, collapses the "
        "scaffolding that mappers duplicated as [NORMALIZE] commits, verifies, and writes the changelog.",
        needs=(_INTEGRATION_BRANCH.name, _CHERRY_PICK_CONFLICTS.name, _SCAFFOLDING_GUIDE.name),
        produces=(_INTEGRATED_BRANCH, _REDUCE_OUTCOME, _REDUCE_JUNIT, _REDUCE_WITNESS_LINKS, _CHANGELOG_ENTRIES),
        work=AgentWork(
            fanout=SingleFanout(description="one agent"),
            prompt_template=NonEmptyStr(node_prompt_template(ANNEALER_TEMPLATE_NAME, variants.annealer)),
            job_variables=_job_variables(ANNEALER_JOB_VARIABLES),
            gates=(_CORPUS_UNTOUCHED_GATE, _LINT_GATE, _PATHS_GATE, _LINKS_GATE, _JUNIT_GATE, _CHANGELOG_GATE),
        ),
    )
    return Pipeline(
        name="witness",
        parameters=_PARAMETERS,
        template_by_name=dict(PACKAGED_TEMPLATE_BY_NAME),
        inputs=(_BASE_COMMIT, _CORPUS),
        nodes=(setup, map_node, review, integrate, reduce),
        outputs=(_INTEGRATED_BRANCH, _REPORT),
    )


WITNESS_PIPELINE: Final[Pipeline] = make_witness_pipeline(no_prompt_variants())

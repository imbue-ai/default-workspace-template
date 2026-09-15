"""What each node of the witness pipeline does, attached to the pipeline's names."""

from pathlib import Path

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr_behaviors.data_types import CorpusScan
from imbue.mngr_behaviors.data_types import WitnessLink
from imbue.mngr_mapreduce.bindings import PipelineBindings
from imbue.mngr_mapreduce.bundle import PUBLISH_OUTPUTS_SNIPPET
from imbue.mngr_mapreduce.execution import Execution
from imbue.mngr_mapreduce.execution import Job
from imbue.mngr_mapreduce.execution import JobResult
from imbue.mngr_mapreduce.execution import OrchestratorNodeProduct
from imbue.mngr_mapreduce.execution import OrchestratorOutcome
from imbue.mngr_mapreduce.executor import make_agent_identity
from imbue.mngr_mapreduce.interfaces import DiscoveredJobsBindingInterface
from imbue.mngr_mapreduce.interfaces import ExecutionObserverInterface
from imbue.mngr_mapreduce.interfaces import OrchestratorNodeBindingInterface
from imbue.mngr_mapreduce.interfaces import PerUpstreamJobBindingInterface
from imbue.mngr_mapreduce.interfaces import SingleJobBindingInterface
from imbue.mngr_mapreduce.launching import REDUCER_INPUTS_DIRNAME
from imbue.mngr_mapreduce.manifest import ManifestWriter
from imbue.mngr_mapreduce.pipeline import Node
from imbue.mngr_mapreduce.primitives import NodeName
from imbue.mngr_mapreduce.utils import sanitize_for_agent_name
from imbue.mngr_witness.conventions import project_of_path
from imbue.mngr_witness.corpus import FeatureTask
from imbue.mngr_witness.corpus import UnitSelection
from imbue.mngr_witness.corpus import discover_feature_tasks
from imbue.mngr_witness.corpus import scan_valid_corpus
from imbue.mngr_witness.gates import make_gate_set
from imbue.mngr_witness.gitops import GitOperationError
from imbue.mngr_witness.gitops import changed_paths
from imbue.mngr_witness.gitops import cherry_pick_branch
from imbue.mngr_witness.gitops import create_branch
from imbue.mngr_witness.outcomes import ARCHIVE_TEST_OUTPUT_DIRNAME
from imbue.mngr_witness.outcomes import OUTCOME_FILENAME
from imbue.mngr_witness.outcomes import OutcomeInvalidError
from imbue.mngr_witness.outcomes import OutcomeMissingError
from imbue.mngr_witness.outcomes import SCAFFOLDING_GUIDE_FILENAME
from imbue.mngr_witness.outcomes import read_witness_links
from imbue.mngr_witness.pipeline import INTEGRATE_NODE_NAME
from imbue.mngr_witness.pipeline import MAP_NODE_NAME
from imbue.mngr_witness.pipeline import REDUCE_NODE_NAME
from imbue.mngr_witness.pipeline import REVIEW_NODE_NAME
from imbue.mngr_witness.pipeline import SETUP_NODE_NAME
from imbue.mngr_witness.prompts import AnnealerPromptContext
from imbue.mngr_witness.prompts import ExistingWitness
from imbue.mngr_witness.prompts import MapperPromptContext
from imbue.mngr_witness.prompts import PromptVariants
from imbue.mngr_witness.prompts import ReviewerPromptContext
from imbue.mngr_witness.prompts import RunFacts
from imbue.mngr_witness.prompts import ScaffoldPromptContext
from imbue.mngr_witness.prompts import UnintegratedBranch
from imbue.mngr_witness.prompts import annealer_job_variables
from imbue.mngr_witness.prompts import mapper_job_variables
from imbue.mngr_witness.prompts import reviewer_job_variables
from imbue.mngr_witness.prompts import run_parameters
from imbue.mngr_witness.prompts import scaffold_job_variables

SCAFFOLD_JOB_SLUG = "scaffold"
# The annealer's job slug names the deliverable branch, <pipeline>/<execution>/reduce/integrated; the
# orchestrator's intermediate cherry-pick target is <pipeline>/<execution>/integrate/integration.
ANNEAL_JOB_SLUG = "integrated"
INTEGRATION_JOB_SLUG = "integration"


class IntegrationMissingError(MngrError):
    """Raised when the reduce node is asked for its job before the integrate node produced a branch."""

    ...


class WitnessRun(FrozenModel):
    """What every witness binding needs to know about the run: the corpus, its selected units, and where things go."""

    corpus_root: Path = Field(description="The corpus root, relative to the repo root")
    test_roots: tuple[Path, ...] = Field(description="The test roots the matrix harvests, relative to the repo root")
    generation_root: Path = Field(description="Where new witness modules go, relative to the repo root")
    scan: CorpusScan = Field(description="The validated corpus")
    tasks: tuple[FeatureTask, ...] = Field(description="The selected units grouped by feature file")
    changelog_branch: str = Field(description="The name changelog entries carry")
    variants: PromptVariants = Field(description="Per-node prompt overrides")

    @property
    def coordinates_by_slug(self) -> dict[str, tuple[str, ...]]:
        return {task.slug: task.coordinates for task in self.tasks}


def make_witness_run(
    repo_root: Path,
    corpus_root: Path,
    test_roots: tuple[Path, ...],
    generation_root: Path,
    selection: UnitSelection,
    changelog_branch: str,
    variants: PromptVariants,
) -> WitnessRun:
    """Scan the corpus, refuse a broken one, and group the selected units into feature tasks."""
    scan = scan_valid_corpus(repo_root / corpus_root)
    tasks = discover_feature_tasks(scan, repo_root / corpus_root, selection)
    return WitnessRun(
        corpus_root=corpus_root,
        test_roots=test_roots,
        generation_root=generation_root,
        scan=scan,
        tasks=tuple(tasks),
        changelog_branch=changelog_branch,
        variants=variants,
    )


@pure
def run_facts(run: WitnessRun) -> RunFacts:
    return RunFacts(
        corpus_root=run.corpus_root.as_posix(),
        test_roots=tuple(root.as_posix() for root in run.test_roots),
        inputs_dirname=REDUCER_INPUTS_DIRNAME,
        publish_snippet=PUBLISH_OUTPUTS_SNIPPET,
        changelog_branch=run.changelog_branch,
        generation_root=run.generation_root.as_posix(),
    )


@pure
def execution_context(run: WitnessRun) -> dict[str, str]:
    """The pipeline's parameters as this run supplies them, available to every node's prompt template."""
    return run_parameters(run_facts(run))


@pure
def branch_name_for(execution: Execution, node_name: NodeName, slug: str) -> str:
    """The branch the executor will create for a job, named the way it names them, so prompts can state it."""
    return make_agent_identity(
        execution.pipeline_name, execution.execution_name, node_name, sanitize_for_agent_name(slug)
    ).branch_name


@pure
def scaffold_base_ref(execution: Execution) -> str:
    """Where the map node starts: the scaffolding branch when setup published one, else the base commit."""
    for job_result in execution.successful_job_results_of(SETUP_NODE_NAME):
        if job_result.is_branch_applied:
            return job_result.branch_name
    return execution.base_commit


@pure
def _successful_archive_dirs(execution: Execution, node_name: NodeName) -> tuple[Path, ...]:
    """A successful job always has an archive; the filter only narrows the type."""
    return tuple(
        job_result.archive_dir
        for job_result in execution.successful_job_results_of(node_name)
        if job_result.archive_dir is not None
    )


def _scaffolding_guide(execution: Execution) -> str:
    """The setup node's guide, or an empty guide when setup wrote none."""
    for archive_dir in _successful_archive_dirs(execution, SETUP_NODE_NAME):
        guide_path = archive_dir / ARCHIVE_TEST_OUTPUT_DIRNAME / SCAFFOLDING_GUIDE_FILENAME
        if guide_path.exists():
            return guide_path.read_text()
    return ""


def _base_witness_links(execution: Execution) -> tuple[WitnessLink, ...]:
    """The links the setup node harvested from the tree, so mappers know what is already witnessed."""
    for archive_dir in _successful_archive_dirs(execution, SETUP_NODE_NAME):
        try:
            return read_witness_links(archive_dir)
        except (OutcomeMissingError, OutcomeInvalidError) as exc:
            logger.warning("Ignoring the setup node's witness links: {}", exc)
    return ()


@pure
def generation_module_for(run: WitnessRun, task: FeatureTask) -> Path:
    """``<generation_root>/<folders>/<feature basename>_test.py`` for a feature file."""
    return run.generation_root / task.relative_path.parent / f"{task.relative_path.stem.replace('-', '_')}_test.py"


@pure
def reviewed_tip(job_result: JobResult) -> str:
    """The commit a reviewed job's work ends at: its branch, or where it started when the reviewer committed nothing."""
    return job_result.branch_name if job_result.is_branch_applied else job_result.job.base_ref


@pure
def integration_branch_of(execution: Execution) -> str:
    """The branch the integrate node produced; the reduce node cannot start without it."""
    outcome = execution.node_outcome_by_node_name.get(INTEGRATE_NODE_NAME)
    if outcome is None or not isinstance(outcome.produced, OrchestratorNodeProduct):
        raise IntegrationMissingError("The reduce node has nothing to start from: the integrate node has not run")
    branch_name = outcome.produced.outcome.branch_name
    if branch_name is None:
        raise IntegrationMissingError("The reduce node has nothing to start from: the integrate node made no branch")
    return branch_name


class ScaffoldBinding(SingleJobBindingInterface):
    """One job from the base commit: find or make the scaffolding every mapper will use."""

    witness_run: WitnessRun = Field(frozen=True, description="The run the binding serves")

    def build_job(self, execution: Execution, node: Node) -> Job:
        context = ScaffoldPromptContext(
            branch_name=branch_name_for(execution, SETUP_NODE_NAME, SCAFFOLD_JOB_SLUG),
            units=tuple(unit for task in self.witness_run.tasks for unit in task.units),
        )
        return Job(
            slug=SCAFFOLD_JOB_SLUG,
            base_ref=execution.base_commit,
            template_variables=scaffold_job_variables(context),
            inputs_dir=None,
        )


class MapBinding(DiscoveredJobsBindingInterface):
    """One job per feature file, from the scaffolding branch, each told its units, clauses, and existing witnesses."""

    witness_run: WitnessRun = Field(frozen=True, description="The run the binding serves")

    def discover_jobs(self, execution: Execution, node: Node) -> list[Job]:
        base_ref = scaffold_base_ref(execution)
        guide = _scaffolding_guide(execution)
        links = _base_witness_links(execution)
        jobs: list[Job] = []
        for task in self.witness_run.tasks:
            coordinates = set(task.coordinates)
            existing = tuple(
                ExistingWitness(coordinate=link.coordinate, node_id=link.test, partial=link.partial)
                for link in links
                if link.coordinate is not None and link.coordinate in coordinates
            )
            context = MapperPromptContext(
                branch_name=branch_name_for(execution, MAP_NODE_NAME, task.slug),
                feature_path=(self.witness_run.corpus_root / task.relative_path).as_posix(),
                units=task.units,
                existing_witnesses=existing,
                scaffolding_guide_markdown=guide,
                generation_module=generation_module_for(self.witness_run, task).as_posix(),
            )
            jobs.append(
                Job(
                    slug=task.slug,
                    base_ref=base_ref,
                    template_variables=mapper_job_variables(context),
                    inputs_dir=None,
                )
            )
        return jobs


class ReviewBinding(PerUpstreamJobBindingInterface):
    """One job per successful mapper, from where its work ended, handed the mapper's archive to check.

    A mapper that committed nothing is still reviewed: its verdicts claim the
    tree already witnesses its units, and that claim is what the reviewer checks.
    """

    witness_run: WitnessRun = Field(frozen=True, description="The run the binding serves")

    def build_job(self, execution: Execution, node: Node, upstream_result: JobResult) -> Job | None:
        task = self._task_of(upstream_result)
        if task is None or upstream_result.archive_dir is None:
            logger.debug("Not reviewing job '{}': no task or archive to review", upstream_result.job.slug)
            return None
        outcome_path = upstream_result.archive_dir / ARCHIVE_TEST_OUTPUT_DIRNAME / OUTCOME_FILENAME
        context = ReviewerPromptContext(
            branch_name=branch_name_for(execution, REVIEW_NODE_NAME, task.slug),
            feature_path=(self.witness_run.corpus_root / task.relative_path).as_posix(),
            units=task.units,
            mapper_branch=upstream_result.branch_name if upstream_result.is_branch_applied else None,
            mapper_outcome_json=outcome_path.read_text(),
        )
        return Job(
            slug=task.slug,
            base_ref=reviewed_tip(upstream_result),
            template_variables=reviewer_job_variables(context),
            inputs_dir=upstream_result.archive_dir,
        )

    def _task_of(self, upstream_result: JobResult) -> FeatureTask | None:
        for task in self.witness_run.tasks:
            if task.slug == upstream_result.job.slug:
                return task
        return None


class IntegrateBinding(OrchestratorNodeBindingInterface):
    """Cherry-pick every reviewed branch onto the scaffolding commit, recording what conflicted."""

    witness_run: WitnessRun = Field(frozen=True, description="The run the binding serves")
    source_dir: Path = Field(frozen=True, description="The operator checkout")
    cg: ConcurrencyGroup = Field(frozen=True, description="Runs git")
    unintegrated: list[UnintegratedBranch] = Field(
        default_factory=list, description="Branches whose cherry-pick conflicted"
    )
    integrated: list[str] = Field(default_factory=list, description="Branches cherry-picked cleanly, in order")

    def run(self, execution: Execution, node: Node) -> OrchestratorOutcome:
        integration_branch = branch_name_for(execution, INTEGRATE_NODE_NAME, INTEGRATION_JOB_SLUG)
        scaffold_base = scaffold_base_ref(execution)
        try:
            create_branch(self.cg, self.source_dir, integration_branch, scaffold_base)
        except GitOperationError as exc:
            return OrchestratorOutcome(
                branch_name=None, summary="", error_summary=f"Could not create {integration_branch}: {exc}"
            )
        for job_result in execution.successful_job_results_of(REVIEW_NODE_NAME):
            tip = reviewed_tip(job_result)
            if tip == scaffold_base:
                logger.debug(
                    "Nothing to integrate from job '{}': neither mapper nor reviewer committed", job_result.job.slug
                )
                continue
            conflicting = cherry_pick_branch(self.cg, self.source_dir, integration_branch, scaffold_base, tip)
            if conflicting:
                self.unintegrated.append(UnintegratedBranch(branch_name=tip, conflicting_paths=tuple(conflicting)))
            else:
                self.integrated.append(tip)
        summary = (
            f"Cherry-picked {len(self.integrated)} branch(es) onto {integration_branch}; "
            f"{len(self.unintegrated)} conflicted"
        )
        return OrchestratorOutcome(branch_name=integration_branch, summary=summary, error_summary=None)


class AnnealBinding(SingleJobBindingInterface):
    """One job from the integration branch: integrate what conflicted, collapse duplication, verify, write the changelog."""

    witness_run: WitnessRun = Field(frozen=True, description="The run the binding serves")
    source_dir: Path = Field(frozen=True, description="The operator checkout")
    cg: ConcurrencyGroup = Field(frozen=True, description="Runs git")
    integrate: IntegrateBinding = Field(
        frozen=True, description="The integrate node's binding, whose results this job continues"
    )

    def build_job(self, execution: Execution, node: Node) -> Job:
        integration_branch = integration_branch_of(execution)
        # Measured from the execution's base commit, as the changelog gate measures, so a project only the
        # setup node touched still owes an entry.
        touched = changed_paths(self.cg, self.source_dir, execution.base_commit, integration_branch)
        context = AnnealerPromptContext(
            branch_name=branch_name_for(execution, REDUCE_NODE_NAME, ANNEAL_JOB_SLUG),
            integrated_branches=tuple(self.integrate.integrated),
            unintegrated=tuple(self.integrate.unintegrated),
            integration_branch=integration_branch,
            touched_projects=tuple(sorted({project_of_path(path) for path in touched})),
            changelog_branch=self.witness_run.changelog_branch,
            scaffolding_guide_markdown=_scaffolding_guide(execution),
        )
        return Job(
            slug=ANNEAL_JOB_SLUG,
            base_ref=integration_branch,
            template_variables=annealer_job_variables(context),
            inputs_dir=execution.plan.output_dir,
        )


def make_witness_bindings(
    run: WitnessRun,
    source_dir: Path,
    output_dir: Path,
    cg: ConcurrencyGroup,
    base_commit: str,
    observers: tuple[ExecutionObserverInterface, ...],
) -> PipelineBindings:
    """Bind every node and gate of the witness pipeline; the manifest writer is always among the observers."""
    integrate = IntegrateBinding(witness_run=run, source_dir=source_dir, cg=cg)
    gates = make_gate_set(
        corpus_root=run.corpus_root,
        unit_coordinates=frozenset(unit.coordinate for unit in run.scan.units),
        clauses_by_coordinate={unit.coordinate: unit.clauses for task in run.tasks for unit in task.units},
        coordinates_by_slug=run.coordinates_by_slug,
        changelog_branch=run.changelog_branch,
        execution_base_commit=base_commit,
        cg=cg,
    )
    return PipelineBindings(
        agent_binding_by_node_name={
            SETUP_NODE_NAME: ScaffoldBinding(witness_run=run),
            MAP_NODE_NAME: MapBinding(witness_run=run),
            REVIEW_NODE_NAME: ReviewBinding(witness_run=run),
            REDUCE_NODE_NAME: AnnealBinding(witness_run=run, source_dir=source_dir, cg=cg, integrate=integrate),
        },
        orchestrator_binding_by_node_name={INTEGRATE_NODE_NAME: integrate},
        gate_binding_by_gate_name=gates.binding_by_gate_name,
        observers=(ManifestWriter(output_dir=output_dir), *observers),
    )

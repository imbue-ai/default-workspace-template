"""The pipeline executor: one loop that runs a whole graph.

``graphlib.TopologicalSorter`` supplies readiness and cycle detection and the
executor supplies everything else. Concurrency is structural and parallelism is
a cap: every node whose dependencies are met is eligible, and the plan decides
how many actually run. Subclasses supply only how agents get hosts, get
launched, and get their outputs back.
"""

import graphlib
import time
from abc import abstractmethod
from collections.abc import Callable
from collections.abc import Sequence
from concurrent.futures import Future
from pathlib import Path
from typing import Final
from typing import TypeVar
from typing import assert_never

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import AgentName
from imbue.mngr_mapreduce.bindings import PipelineBindings
from imbue.mngr_mapreduce.bindings import UnboundPipelineError
from imbue.mngr_mapreduce.bindings import assert_pipeline_is_bound
from imbue.mngr_mapreduce.execution import AgentIdentity
from imbue.mngr_mapreduce.execution import AgentNodeProduct
from imbue.mngr_mapreduce.execution import BranchFetch
from imbue.mngr_mapreduce.execution import Execution
from imbue.mngr_mapreduce.execution import ExecutionPlan
from imbue.mngr_mapreduce.execution import GateResult
from imbue.mngr_mapreduce.execution import GateSubject
from imbue.mngr_mapreduce.execution import Job
from imbue.mngr_mapreduce.execution import JobResult
from imbue.mngr_mapreduce.execution import LaunchedAgent
from imbue.mngr_mapreduce.execution import NodeHosts
from imbue.mngr_mapreduce.execution import NodeOutcome
from imbue.mngr_mapreduce.execution import NodeStatus
from imbue.mngr_mapreduce.execution import OrchestratorNodeProduct
from imbue.mngr_mapreduce.execution import OrchestratorOutcome
from imbue.mngr_mapreduce.execution import evaluate_agent_node
from imbue.mngr_mapreduce.interfaces import DiscoveredJobsBindingInterface
from imbue.mngr_mapreduce.interfaces import PerPartitionBindingInterface
from imbue.mngr_mapreduce.interfaces import PerUpstreamJobBindingInterface
from imbue.mngr_mapreduce.interfaces import PipelineExecutorInterface
from imbue.mngr_mapreduce.interfaces import SingleJobBindingInterface
from imbue.mngr_mapreduce.pipeline import AgentWork
from imbue.mngr_mapreduce.pipeline import FanoutKind
from imbue.mngr_mapreduce.pipeline import Gate
from imbue.mngr_mapreduce.pipeline import Node
from imbue.mngr_mapreduce.pipeline import OrchestratorWork
from imbue.mngr_mapreduce.pipeline import Pipeline
from imbue.mngr_mapreduce.pipeline import PipelineInvariantError
from imbue.mngr_mapreduce.pipeline import UpstreamFanout
from imbue.mngr_mapreduce.pipeline import make_topological_sorter
from imbue.mngr_mapreduce.pipeline import producing_node_name_by_artifact_name
from imbue.mngr_mapreduce.primitives import NodeName
from imbue.mngr_mapreduce.primitives import PartitionKey
from imbue.mngr_mapreduce.prompts import render_prompt
from imbue.mngr_mapreduce.utils import dedup_name
from imbue.mngr_mapreduce.utils import pause
from imbue.mngr_mapreduce.utils import sanitize_for_agent_name

_BindingT = TypeVar("_BindingT")

_ResultT = TypeVar("_ResultT")

_TIMEOUT_ERROR_SUMMARY: Final[str] = "Agent was stopped because the timeout was reached."
_UNPULLED_ERROR_SUMMARY: Final[str] = "Agent published an archive that could not be pulled or extracted."
_UNFETCHED_ERROR_SUMMARY: Final[str] = "Agent published a branch bundle that could not be applied to the source repo."


class AgentLaunchError(MngrError):
    """Raised by an executor when an agent could not be created or prompted for a job."""

    ...


class PlanMismatchError(MngrError, ValueError):
    """Raised when an execution plan places a node the pipeline does not have."""

    ...


class UnsuppliedParameterError(MngrError, ValueError):
    """Raised when an execution does not supply a pipeline parameter that has no default."""

    ...


class BindingFailedError(MngrError):
    """Raised when plugin-supplied behaviour -- a binding, an orchestrator step, a gate -- raised."""

    ...


def call_binding(description: str, call: Callable[[], _ResultT]) -> _ResultT:
    """Invoke plugin-supplied behaviour, turning whatever it raises into a BindingFailedError.

    A binding, an orchestrator step and a gate are written by whoever builds the
    pipeline, so no exception type this package can name bounds what they raise.
    Nothing is swallowed: the original is chained and its text reaches the node's
    outcome, which is what lets a raising step fail its own node and not the run.
    """
    try:
        return call()
    except BindingFailedError:
        raise
    except Exception as exc:
        raise BindingFailedError(f"{description} raised {type(exc).__name__}: {exc}") from exc


@pure
def find_unplanned_node_names(pipeline: Pipeline, plan: ExecutionPlan) -> list[str]:
    """The node names the plan overrides that the pipeline does not define."""
    node_names = {node.name for node in pipeline.nodes}
    return sorted(str(name) for name in plan.placement_by_node_name if name not in node_names)


def assert_plan_fits_pipeline(pipeline: Pipeline, plan: ExecutionPlan) -> None:
    """Raise PlanMismatchError before any agent launches if the plan names nodes the pipeline lacks."""
    unplanned = find_unplanned_node_names(pipeline, plan)
    if unplanned:
        raise PlanMismatchError(
            f"Execution plan places nodes that pipeline '{pipeline.name}' does not have: " + ", ".join(unplanned)
        )


@pure
def resolve_context(pipeline: Pipeline, context: dict[str, str]) -> dict[str, str]:
    """The execution's context with each parameter the execution omitted filled in from its default."""
    defaults = {
        str(parameter.name): parameter.default for parameter in pipeline.parameters if parameter.default is not None
    }
    return {**defaults, **context}


@pure
def find_unsupplied_parameter_names(pipeline: Pipeline, context: dict[str, str]) -> list[str]:
    """The pipeline's parameters that have no default and that the context does not supply."""
    return sorted(
        str(parameter.name)
        for parameter in pipeline.parameters
        if parameter.default is None and str(parameter.name) not in context
    )


def assert_context_supplies_every_parameter(pipeline: Pipeline, context: dict[str, str]) -> None:
    """Raise UnsuppliedParameterError before any agent launches if a prompt would render against a missing name."""
    unsupplied = find_unsupplied_parameter_names(pipeline, context)
    if unsupplied:
        raise UnsuppliedParameterError(
            f"Execution of pipeline '{pipeline.name}' does not supply " + ", ".join(unsupplied)
        )


@pure
def make_agent_identity(
    pipeline_name: str,
    execution_name: str,
    node_name: NodeName,
    suffix: str,
) -> AgentIdentity:
    """Name a job's agent ``<pipeline>-<execution>-<node>-<suffix>`` and its branch ``<pipeline>/<execution>/<node>/<suffix>``."""
    return AgentIdentity(
        agent_name=AgentName(f"{pipeline_name}-{execution_name}-{node_name}-{suffix}"),
        branch_name=f"{pipeline_name}/{execution_name}/{node_name}/{suffix}",
    )


class RunningNode(MutableModel):
    """One node the executor has started and has not yet judged."""

    node: Node = Field(description="The node being run")
    hosts: NodeHosts | None = Field(default=None, description="What provision_hosts returned, or None for no agents")
    queued_jobs: list[Job] = Field(default_factory=list, description="Jobs not yet launched, in fan-out order")
    launched_agents: list[LaunchedAgent] = Field(default_factory=list, description="Agents still being waited on")
    job_results: list[JobResult] = Field(default_factory=list, description="Results collected so far")
    orchestrator_outcome: OrchestratorOutcome | None = Field(
        default=None, description="Set as soon as an orchestrator node's binding returns"
    )
    start_error: str | None = Field(default=None, description="Why the node could not be started at all, or None")
    used_suffixes: set[str] = Field(default_factory=set, description="Agent name suffixes already taken in this node")
    launched_count: int = Field(
        default=0, description="How many jobs the node has launched, which places the next one"
    )

    @property
    def is_settled(self) -> bool:
        """Nothing left to launch and nothing left to wait for."""
        return not self.queued_jobs and not self.launched_agents


class AbstractPipelineExecutor(PipelineExecutorInterface):
    """Runs a pipeline's graph under an execution plan; subclasses supply how agents get hosts, get launched, and report back.

    The scheduling loop, fan-out arithmetic, job naming, prompt rendering,
    launching, polling, timeouts, archive and branch collection, gating,
    node evaluation and halting are the same everywhere and live here.
    The abstract methods are the whole surface a subclass supplies.
    """

    pipeline: Pipeline = Field(frozen=True, description="What to run")
    bindings: PipelineBindings = Field(frozen=True, description="What each node does and what each gate checks")
    concurrency_group: ConcurrencyGroup = Field(frozen=True, description="Parent of the launch worker pools")
    execution: Execution = Field(description="The execution so far; replaced after every change")
    admitted_node_names: list[NodeName] = Field(
        default_factory=list,
        description="Nodes whose dependencies are met but which the parallelism cap has not let start",
    )
    running_node_by_name: dict[NodeName, RunningNode] = Field(
        default_factory=dict, description="Nodes started and not yet judged"
    )

    @abstractmethod
    def provision_hosts(self, node: Node, job_count: int) -> NodeHosts:
        """Make hosts ready for a node's agents; the token returned is handed to launch_agent and release_hosts."""

    @abstractmethod
    def launch_agent(
        self,
        node: Node,
        job: Job,
        prompt: str,
        identity: AgentIdentity,
        hosts: NodeHosts,
        job_idx: int,
    ) -> LaunchedAgent:
        """Create the agent for one job and deliver its prompt; raise AgentLaunchError when that fails."""

    @abstractmethod
    def is_published(self, agent: LaunchedAgent) -> bool:
        """Whether the agent has finished writing its outputs archive."""

    @abstractmethod
    def pull_outputs(self, agent: LaunchedAgent, destination_dir: Path) -> Path | None:
        """Extract the agent's outputs archive under destination_dir and return where, or None when that fails."""

    @abstractmethod
    def fetch_branch(self, agent: LaunchedAgent, archive_dir: Path) -> BranchFetch:
        """Bring the agent's published branch into the source repo, and say what came of it.

        Committing nothing and failing to apply a bundle are different outcomes:
        the first is a legitimate no-op, the second loses the agent's work.
        """

    @abstractmethod
    def stop_agent(self, agent: LaunchedAgent) -> None:
        """Stop the agent; it has published or timed out and is no longer needed."""

    @abstractmethod
    def release_hosts(self, hosts: NodeHosts) -> None:
        """Give back whatever provision_hosts made for the node."""

    def execute(self) -> Execution:
        assert_pipeline_is_bound(self.pipeline, self.bindings)
        assert_plan_fits_pipeline(self.pipeline, self.execution.plan)
        context = resolve_context(self.pipeline, self.execution.context)
        assert_context_supplies_every_parameter(self.pipeline, context)
        self._update_execution(
            self.execution.model_copy_update(to_update(self.execution.field_ref().context, context))
        )
        sorter = make_topological_sorter(self.pipeline)
        sorter.prepare()
        is_completed = False
        try:
            execution = self._run_every_node(sorter)
            is_completed = True
            return execution
        finally:
            if not is_completed:
                # A finally rather than an except, so a Ctrl-C hours into a node cleans
                # up too, and nothing has to be caught to do it.
                self._halt("The execution did not run to completion")

    def _run_every_node(self, sorter: "graphlib.TopologicalSorter[NodeName]") -> Execution:
        """Walk the graph until every node has ended or a failure halts the run."""
        while sorter.is_active():
            self.admitted_node_names.extend(sorter.get_ready())
            self._start_admitted_nodes()
            self._launch_up_to_the_caps()
            self._poll_running_agents_once()
            for node_name in self._settled_node_names():
                outcome = self._finish_node(node_name)
                if outcome.status is NodeStatus.FAILED:
                    return self._halt(f"Node '{node_name}' failed: {outcome.detail}")
                sorter.done(node_name)
            if self._is_waiting_on_agents():
                pause(self.execution.plan.poll_interval_seconds)
        return self.execution

    def _start_admitted_nodes(self) -> None:
        """Start admitted nodes up to the plan's parallelism cap, in the order they became eligible."""
        while self.admitted_node_names and self._has_room_for_another_node():
            node_name = self.admitted_node_names.pop(0)
            self._start_node(_find_node(self.pipeline, node_name))

    def _has_room_for_another_node(self) -> bool:
        cap = self.execution.plan.max_concurrent_nodes
        return cap == 0 or len(self.running_node_by_name) < cap

    def _start_node(self, node: Node) -> None:
        running = RunningNode(node=node)
        self.running_node_by_name[node.name] = running
        match node.work:
            case OrchestratorWork():
                self._run_orchestrator_node(running)
            case AgentWork() as work:
                self._begin_agent_node(running, work)
            case _ as unreachable:
                assert_never(unreachable)

    def _run_orchestrator_node(self, running: RunningNode) -> None:
        """Run an orchestrator binding inline.

        It runs on the executor's own loop, which is what gives it the operator
        checkout to itself: nothing else touches the checkout while it works.
        The loop is not polling meanwhile, which is harmless because a published
        archive is collected whenever the loop next looks.
        """
        binding = self.bindings.orchestrator_binding_by_node_name[running.node.name]
        with log_span("Running orchestrator node '{}'", running.node.name):
            try:
                running.orchestrator_outcome = call_binding(
                    f"Orchestrator node '{running.node.name}'", lambda: binding.run(self.execution, running.node)
                )
            except BindingFailedError as exc:
                logger.warning("Orchestrator node '{}' raised: {}", running.node.name, exc)
                running.orchestrator_outcome = OrchestratorOutcome(
                    branch_name=None, summary="", error_summary=f"Orchestrator node raised: {exc}"
                )

    def _begin_agent_node(self, running: RunningNode, work: AgentWork) -> None:
        """Fan the node out and provision hosts for what it found; a fan-out of zero provisions nothing.

        A binding or a provider that raises fails this node rather than the
        process, so the halt path still runs and everything in flight is stopped.
        """
        try:
            with log_span("Fanning out node '{}'", running.node.name):
                jobs = call_binding(
                    f"The fan-out of node '{running.node.name}'", lambda: self._discover_jobs(running.node, work)
                )
            if not jobs:
                logger.debug("Node '{}' fanned out to no jobs", running.node.name)
                return
            with log_span("Provisioning hosts for {} job(s) of node '{}'", len(jobs), running.node.name):
                running.hosts = call_binding(
                    f"Provisioning hosts for node '{running.node.name}'",
                    lambda: self.provision_hosts(running.node, len(jobs)),
                )
        except BindingFailedError as exc:
            logger.warning("Node '{}' could not be started: {}", running.node.name, exc)
            running.start_error = f"The node could not be started: {exc}"
            return
        running.queued_jobs = jobs

    def _discover_jobs(self, node: Node, work: AgentWork) -> list[Job]:
        """The node's jobs, with the framework doing whichever fan-out the node declared."""
        binding = self.bindings.agent_binding_by_node_name[node.name]
        match work.fanout.kind:
            case FanoutKind.SINGLE:
                return [_as_binding(binding, SingleJobBindingInterface, node).build_job(self.execution, node)]
            case FanoutKind.DISCOVERED:
                return _as_binding(binding, DiscoveredJobsBindingInterface, node).discover_jobs(self.execution, node)
            case FanoutKind.PER_UPSTREAM_JOB:
                per_upstream = _as_binding(binding, PerUpstreamJobBindingInterface, node)
                built = (
                    per_upstream.build_job(self.execution, node, upstream_result)
                    for upstream_result in self._upstream_successful_results(_upstream_fanout_of(work))
                )
                return [job for job in built if job is not None]
            case FanoutKind.PER_PARTITION:
                return self._partition_jobs(
                    _as_binding(binding, PerPartitionBindingInterface, node), node, _upstream_fanout_of(work)
                )
            case _ as unreachable:
                assert_never(unreachable)

    def _partition_jobs(self, binding: PerPartitionBindingInterface, node: Node, fanout: UpstreamFanout) -> list[Job]:
        """Group the upstream successful results by key, first-seen order, and build one job per group."""
        results_by_key: dict[PartitionKey, list[JobResult]] = {}
        for upstream_result in self._upstream_successful_results(fanout):
            key = binding.partition(self.execution, node, upstream_result)
            results_by_key.setdefault(key, []).append(upstream_result)
        return [
            binding.build_job(self.execution, node, key, tuple(results)) for key, results in results_by_key.items()
        ]

    def _upstream_successful_results(self, fanout: UpstreamFanout) -> tuple[JobResult, ...]:
        """The successful results of the node that produces the artifact the fan-out names."""
        producer = producing_node_name_by_artifact_name(self.pipeline)[fanout.over]
        return self.execution.successful_job_results_of(producer)

    def _launch_up_to_the_caps(self) -> None:
        """Top up every running node's queue, under both the node's cap and the execution's."""
        for running in self.running_node_by_name.values():
            if not running.queued_jobs or running.hosts is None:
                continue
            allowance = self._launch_allowance(running)
            if allowance <= 0:
                continue
            jobs, running.queued_jobs = running.queued_jobs[:allowance], running.queued_jobs[allowance:]
            launched, launch_failures = self._launch_jobs(running, jobs)
            running.launched_agents.extend(launched)
            running.job_results.extend(launch_failures)

    def _launch_allowance(self, running: RunningNode) -> int:
        """How many more of this node's agents may exist right now."""
        node_cap = self.execution.plan.placement_for(running.node.name).max_running_agents
        node_allowance = len(running.queued_jobs) if node_cap == 0 else node_cap - len(running.launched_agents)
        execution_cap = self.execution.plan.max_running_agents
        if execution_cap == 0:
            return max(0, node_allowance)
        running_count = sum(len(other.launched_agents) for other in self.running_node_by_name.values())
        return max(0, min(node_allowance, execution_cap - running_count))

    def _launch_jobs(self, running: RunningNode, jobs: Sequence[Job]) -> tuple[list[LaunchedAgent], list[JobResult]]:
        """Launch the jobs concurrently up to the node's launch parallelism, spacing submissions by the launch delay.

        Identities and prompts are produced on the calling thread before anything
        is submitted, so names are deterministic whatever order the launches
        finish in, and a failed launch becomes a failed job result under the name
        it would have had.
        """
        work = running.node.work
        if not jobs or running.hosts is None or not isinstance(work, AgentWork):
            return [], []
        placement = self.execution.plan.placement_for(running.node.name)
        launched: list[LaunchedAgent] = []
        launch_failures: list[JobResult] = []
        with ConcurrencyGroupExecutor(
            parent_cg=self.concurrency_group,
            name=f"pipeline_launch_{running.node.name}",
            max_workers=placement.max_parallel_launch,
        ) as launcher:
            futures: list[tuple[Job, AgentIdentity, Future[LaunchedAgent]]] = []
            for submission_idx, job in enumerate(jobs):
                if submission_idx > 0 and self.execution.plan.launch_delay_seconds > 0:
                    pause(self.execution.plan.launch_delay_seconds)
                identity, job_idx = self._assign_identity(running, job)
                try:
                    prompt = self._render_job_prompt(running.node, work, job)
                except MngrError as exc:
                    logger.warning("Could not build the prompt for job '{}': {}", job.slug, exc)
                    launch_failures.append(
                        _make_failed_job_result(job, identity, f"Could not build the prompt: {exc}")
                    )
                    continue
                futures.append(
                    (
                        job,
                        identity,
                        launcher.submit(
                            self.launch_agent, running.node, job, prompt, identity, running.hosts, job_idx
                        ),
                    )
                )
            for job, identity, future in futures:
                try:
                    launched.append(future.result())
                except (AgentLaunchError, BaseExceptionGroup) as exc:
                    logger.warning("Failed to launch agent '{}' for job '{}': {}", identity.agent_name, job.slug, exc)
                    launch_failures.append(_make_failed_job_result(job, identity, f"Failed to launch agent: {exc}"))
        return launched, launch_failures

    def _render_job_prompt(self, node: Node, work: AgentWork, job: Job) -> str:
        """Render the node's template against the execution context overlaid with the job's variables."""
        missing = sorted(str(name) for name in work.job_variables if str(name) not in job.template_variables)
        if missing:
            raise UnboundPipelineError(
                f"Job '{job.slug}' of node '{node.name}' did not supply {', '.join(missing)}, "
                f"which the node declares as job variables"
            )
        return render_prompt(
            work.prompt_template,
            self.pipeline.template_by_name,
            {**self.execution.context, **job.template_variables},
        )

    def _assign_identity(self, running: RunningNode, job: Job) -> tuple[AgentIdentity, int]:
        """Name the job's agent and hand it the next launch index of its node.

        Claims the agent name suffix here, where the set of taken suffixes lives,
        so two slugs that sanitize alike still get distinct agents and branches.
        """
        identity = make_agent_identity(
            pipeline_name=self.execution.pipeline_name,
            execution_name=self.execution.execution_name,
            node_name=running.node.name,
            suffix=dedup_name(sanitize_for_agent_name(job.slug), running.used_suffixes),
        )
        job_idx = running.launched_count
        running.launched_count += 1
        return identity, job_idx

    def _poll_running_agents_once(self) -> None:
        """One pass over every in-flight agent of every running node."""
        for running in self.running_node_by_name.values():
            still_pending, finished = self._poll_once(
                running.launched_agents, self.execution.plan.placement_for(running.node.name).agent_timeout_seconds
            )
            running.launched_agents = still_pending
            running.job_results.extend(finished)

    def _poll_once(
        self, pending: Sequence[LaunchedAgent], agent_timeout_seconds: float
    ) -> tuple[list[LaunchedAgent], list[JobResult]]:
        """Collect the published, stop the timed out, keep the rest.

        Published is checked before the deadline on purpose: a deadline says how
        long the executor will wait, not what time it is, so an archive that is
        there when we look counts however late we looked.
        """
        now = time.monotonic()
        still_pending: list[LaunchedAgent] = []
        finished: list[JobResult] = []
        for agent in pending:
            if self.is_published(agent):
                finished.append(self._collect(agent))
            elif now - agent.created_at_monotonic >= agent_timeout_seconds:
                logger.warning("Stopped agent '{}' because its timeout was reached", agent.agent_name)
                self.stop_agent(agent)
                finished.append(_make_failed_job_result(agent.job, _identity_of(agent), _TIMEOUT_ERROR_SUMMARY))
            else:
                still_pending.append(agent)
        return still_pending, finished

    def _collect(self, agent: LaunchedAgent) -> JobResult:
        """Pull a published agent's archive and branch, then stop it."""
        with log_span("Collecting the outputs of agent '{}'", agent.agent_name):
            archive_dir = self.pull_outputs(agent, self.execution.plan.output_dir / str(agent.node_name))
            fetch = self.fetch_branch(agent, archive_dir) if archive_dir is not None else BranchFetch.NONE_PUBLISHED
            self.stop_agent(agent)
        return JobResult(
            job=agent.job,
            agent_name=agent.agent_name,
            branch_name=agent.branch_name,
            archive_dir=archive_dir,
            is_branch_applied=fetch is BranchFetch.APPLIED,
            error_summary=_collection_error(archive_dir, fetch),
            gate_results=(),
        )

    def _settled_node_names(self) -> list[NodeName]:
        return [name for name, running in self.running_node_by_name.items() if running.is_settled]

    def _is_waiting_on_agents(self) -> bool:
        return any(running.launched_agents or running.queued_jobs for running in self.running_node_by_name.values())

    def _finish_node(self, node_name: NodeName) -> NodeOutcome:
        """Gate what came back, judge the node, record it, and give its hosts back."""
        running = self.running_node_by_name.pop(node_name)
        outcome = self._judge(running)
        if running.hosts is not None:
            with log_span("Releasing the hosts of node '{}'", node_name):
                self.release_hosts(running.hosts)
        self._update_execution(self.execution.with_node_outcome(outcome))
        return outcome

    def _judge(self, running: RunningNode) -> NodeOutcome:
        if running.start_error is not None:
            return NodeOutcome(
                node_name=running.node.name,
                status=NodeStatus.FAILED,
                produced=_halted_product(running.node, running, running.start_error),
                detail=running.start_error,
            )
        work = running.node.work
        if not isinstance(work, AgentWork):
            return _judge_orchestrator_node(running)
        try:
            gated = tuple(self._apply_gates(running.node, work, job_result) for job_result in running.job_results)
        except BindingFailedError as exc:
            logger.warning("A gate of node '{}' raised: {}", running.node.name, exc)
            return NodeOutcome(
                node_name=running.node.name,
                status=NodeStatus.FAILED,
                produced=AgentNodeProduct(job_results=tuple(running.job_results)),
                detail=f"A gate raised, leaving every job of the node undecided: {exc}",
            )
        status = evaluate_agent_node(work, gated)
        return NodeOutcome(
            node_name=running.node.name,
            status=status,
            produced=AgentNodeProduct(job_results=gated),
            detail=_describe_node_judgement(work, gated, status),
        )

    def _apply_gates(self, node: Node, work: AgentWork, job_result: JobResult) -> JobResult:
        """Run the node's gates on a completed job; a job that never completed is returned untouched."""
        if job_result.archive_dir is None or job_result.error_summary is not None:
            return job_result
        subject = GateSubject(
            node_name=node.name,
            job=job_result.job,
            branch_name=job_result.branch_name,
            is_branch_applied=job_result.is_branch_applied,
            archive_dir=job_result.archive_dir,
            source_dir=self.execution.plan.source_dir,
        )
        gate_results = tuple(self._run_gate(gate, subject) for gate in work.gates)
        return job_result.model_copy_update(to_update(job_result.field_ref().gate_results, gate_results))

    def _run_gate(self, gate: Gate, subject: GateSubject) -> GateResult:
        binding = self.bindings.gate_binding_by_gate_name[gate.name]
        with log_span("Checking gate '{}' of node '{}'", gate.name, subject.node_name):
            return call_binding(f"Gate '{gate.name}'", lambda: binding.check(gate, subject))

    def _halt(self, reason: str) -> Execution:
        """Stop everything in flight, give every host back, and mark whatever never ran."""
        logger.warning("Halting the execution: {}", reason)
        interrupted_by_name = dict(self.running_node_by_name)
        for running in interrupted_by_name.values():
            for agent in running.launched_agents:
                self.stop_agent(agent)
            if running.hosts is not None:
                self.release_hosts(running.hosts)
        self.running_node_by_name.clear()
        self.admitted_node_names.clear()
        execution = self.execution.with_stopped_reason(reason)
        for node in self.pipeline.nodes:
            if node.name not in execution.node_outcome_by_node_name:
                interrupted = interrupted_by_name.get(node.name)
                detail = (
                    "The execution halted while this node was running"
                    if interrupted is not None
                    else "The execution halted before this node ran"
                )
                execution = execution.with_node_outcome(
                    NodeOutcome(
                        node_name=node.name,
                        status=NodeStatus.SKIPPED,
                        produced=_halted_product(node, interrupted, detail),
                        detail=detail,
                    )
                )
        self._update_execution(execution)
        return self.execution

    def _update_execution(self, execution: Execution) -> None:
        self.execution = execution
        for observer in self.bindings.observers:
            observer.on_execution_changed(execution)


@pure
def _find_node(pipeline: Pipeline, node_name: NodeName) -> Node:
    for node in pipeline.nodes:
        if node.name == node_name:
            return node
    raise PlanMismatchError(f"Pipeline '{pipeline.name}' has no node named '{node_name}'")


def _as_binding(binding: object, expected_type: type[_BindingT], node: Node) -> _BindingT:
    """Narrow a node's binding to the type its fan-out kind requires.

    ``assert_pipeline_is_bound`` already checked this before anything launched;
    this is what makes the guarantee visible to the type system at the call site.
    """
    if not isinstance(binding, expected_type):
        raise UnboundPipelineError(
            f"Node '{node.name}' has a binding that is a {type(binding).__name__}, "
            f"which is not the shape its fan-out requires"
        )
    return binding


@pure
def _halted_product(
    node: Node, interrupted: "RunningNode | None", detail: str
) -> AgentNodeProduct | OrchestratorNodeProduct:
    """What a node the halt caught produced, in the shape its kind implies.

    A node interrupted mid-flight keeps the results already pulled from it. Their
    archives are on disk under the output directory, so discarding them here would
    make the manifest disagree with what the run actually has.
    """
    match node.work:
        case AgentWork():
            return AgentNodeProduct(job_results=tuple(interrupted.job_results) if interrupted is not None else ())
        case OrchestratorWork():
            outcome = interrupted.orchestrator_outcome if interrupted is not None else None
            return _orchestrator_product(outcome, detail)
        case _ as unreachable:
            assert_never(unreachable)


@pure
def _orchestrator_product(outcome: OrchestratorOutcome | None, detail: str) -> OrchestratorNodeProduct:
    """The product of an orchestrator node the halt caught, keeping its outcome when it had one."""
    if outcome is not None:
        return OrchestratorNodeProduct(outcome=outcome)
    return OrchestratorNodeProduct(outcome=OrchestratorOutcome(branch_name=None, summary=detail, error_summary=None))


@pure
def _judge_orchestrator_node(running: RunningNode) -> NodeOutcome:
    outcome = running.orchestrator_outcome
    if outcome is None:
        outcome = OrchestratorOutcome(
            branch_name=None, summary="", error_summary="The orchestrator node produced no outcome"
        )
    is_failed = outcome.error_summary is not None
    return NodeOutcome(
        node_name=running.node.name,
        status=NodeStatus.FAILED if is_failed else NodeStatus.SUCCEEDED,
        produced=OrchestratorNodeProduct(outcome=outcome),
        detail=outcome.error_summary if is_failed else outcome.summary,
    )


@pure
def _describe_node_judgement(work: AgentWork, job_results: Sequence[JobResult], status: NodeStatus) -> str:
    if not job_results:
        return (
            "The node fanned out to no jobs and succeeded vacuously"
            if status is NodeStatus.SUCCEEDED
            else f"The node fanned out to no jobs but requires at least {work.min_job_count}"
        )
    successful_count = sum(1 for job_result in job_results if job_result.is_successful)
    return (
        f"{successful_count} of {len(job_results)} jobs succeeded, against a required completion of "
        f"{work.required_completion}"
    )


@pure
def _upstream_fanout_of(work: AgentWork) -> UpstreamFanout:
    """Narrow to the fan-out that names an upstream artifact; validation guarantees this kind has one."""
    fanout = work.fanout
    if not isinstance(fanout, UpstreamFanout):
        raise PipelineInvariantError(f"A {fanout.kind} fan-out does not name an upstream artifact")
    return fanout


@pure
def _collection_error(archive_dir: Path | None, fetch: BranchFetch) -> str | None:
    """Why collecting the job's outputs did not fully succeed, or None."""
    if archive_dir is None:
        return _UNPULLED_ERROR_SUMMARY
    if fetch is BranchFetch.FAILED:
        return _UNFETCHED_ERROR_SUMMARY
    return None


@pure
def _identity_of(agent: LaunchedAgent) -> AgentIdentity:
    return AgentIdentity(agent_name=agent.agent_name, branch_name=agent.branch_name)


@pure
def _make_failed_job_result(job: Job, identity: AgentIdentity, error_summary: str) -> JobResult:
    return JobResult(
        job=job,
        agent_name=identity.agent_name,
        branch_name=identity.branch_name,
        archive_dir=None,
        is_branch_applied=False,
        error_summary=error_summary,
        gate_results=(),
    )

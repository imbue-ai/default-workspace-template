"""What one performance of a pipeline is made of, and how a node is judged.

A ``Pipeline`` says what to run; an ``ExecutionPlan`` says where each node runs
and under what limits; an ``Execution`` is the record of one run of the two
together. Nothing here knows how an agent is launched -- that is the executor's
job -- so these types are the vocabulary an observer, a report or a manifest
reads without touching a provider.
"""

import math
from collections.abc import Sequence
from enum import auto
from functools import cached_property
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic import computed_field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.primitives import NonNegativeFloat
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveFloat
from imbue.imbue_common.primitives import PositiveInt
from imbue.imbue_common.pure import pure
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotName
from imbue.mngr_mapreduce.pipeline import AgentWork
from imbue.mngr_mapreduce.pipeline import NodeKind
from imbue.mngr_mapreduce.primitives import NodeName


class NodeStatus(UpperCaseStrEnum):
    """How a node ended.

    There is no separate status for a node that ran nothing: a fan-out of zero
    succeeds vacuously, so emptiness shows up as SUCCEEDED with no job results.
    """

    SUCCEEDED = auto()
    FAILED = auto()
    SKIPPED = auto()


class Job(FrozenModel):
    """One agent's assignment within a node: where it starts, what it is handed, and what its prompt renders from."""

    slug: str = Field(
        description="Distinguishes the job within its node; the last segment of the agent and branch names"
    )
    base_ref: str = Field(description="Commit or branch in the source repo the agent starts from")
    template_variables: dict[str, str] = Field(
        description="Values for the node's job_variables, merged over the execution's context to render the prompt"
    )
    inputs_dir: Path | None = Field(
        default=None,
        description="Local directory copied into the agent's work dir before the prompt is sent, or None",
    )


class BranchFetch(UpperCaseStrEnum):
    """What came of bringing a job's published branch into the operator checkout."""

    APPLIED = auto()
    NONE_PUBLISHED = auto()
    FAILED = auto()


class GateResult(FrozenModel):
    """The verdict of one mechanical gate on one job."""

    gate_name: str = Field(description="The gate that produced the verdict")
    is_passed: bool = Field(description="Whether the job satisfied the gate")
    detail: str = Field(description="What the gate found; the reason when it failed")


class JobResult(FrozenModel):
    """What one job produced, and whether it succeeded."""

    job: Job = Field(description="The assignment the result is for")
    agent_name: AgentName = Field(description="The agent that carried the job")
    branch_name: str = Field(description="The branch the agent was asked to publish")
    archive_dir: Path | None = Field(
        description="Where the outputs archive was extracted, or None when the agent never published one"
    )
    is_branch_applied: bool = Field(description="Whether the published branch was fetched into the source repo")
    error_summary: str | None = Field(description="Why the job did not complete normally, or None")
    gate_results: tuple[GateResult, ...] = Field(description="One verdict per gate of the job's node")

    @computed_field
    @cached_property
    def is_successful(self) -> bool:
        """Published an archive, recorded no error, and passed every gate its node declares."""
        is_completed = self.archive_dir is not None and self.error_summary is None
        return is_completed and all(gate_result.is_passed for gate_result in self.gate_results)


class GateSubject(FrozenModel):
    """What a gate examines: one job's extracted archive, and its branch when it made one."""

    node_name: NodeName = Field(description="The node whose gates are being run")
    job: Job = Field(description="The assignment the subject came from")
    branch_name: str = Field(description="The job's branch in the source repo; only present when is_branch_applied")
    is_branch_applied: bool = Field(
        description="Whether the branch is in the source repo. False when the agent committed nothing, which is "
        "legitimate: a gate that inspects the branch must decide for itself what a branch-less job means"
    )
    archive_dir: Path = Field(description="The job's extracted outputs archive")
    source_dir: Path = Field(description="The operator checkout the branch lives in")


class OrchestratorOutcome(FrozenModel):
    """What an orchestrator node produced: at most one branch, plus what happened."""

    branch_name: str | None = Field(description="The branch the node created in the source repo, if any")
    summary: str = Field(description="What the node did, for the report")
    error_summary: str | None = Field(description="Why the node failed, or None")


class AgentNodeProduct(FrozenModel):
    """What an agent node produced: one result per job it launched, which may be none."""

    kind: Literal[NodeKind.AGENT] = NodeKind.AGENT
    job_results: tuple[JobResult, ...] = Field(default=(), description="One result per launched job")

    @computed_field
    @cached_property
    def successful_job_results(self) -> tuple[JobResult, ...]:
        return tuple(job_result for job_result in self.job_results if job_result.is_successful)


class OrchestratorNodeProduct(FrozenModel):
    """What an orchestrator node produced: exactly one outcome."""

    kind: Literal[NodeKind.ORCHESTRATOR] = NodeKind.ORCHESTRATOR
    outcome: OrchestratorOutcome = Field(description="What the node did in the executor's own process")


class NodeOutcome(FrozenModel):
    """What one node produced and how it was judged."""

    node_name: NodeName = Field(description="The node the outcome belongs to")
    status: NodeStatus = Field(description="How the node ended")
    produced: AgentNodeProduct | OrchestratorNodeProduct = Field(
        discriminator="kind", description="What the node produced, in the shape its kind implies"
    )
    detail: str = Field(default="", description="Why the node ended as it did, for the report")


class NodePlacement(FrozenModel):
    """Where one node's agents run and under what limits; the unit of an execution plan."""

    provider: ProviderInstanceName = Field(description="The mngr provider the node's agents are created on")
    agent_type: AgentTypeName = Field(description="The agent type to launch")
    env_options: AgentEnvironmentOptions = Field(
        default_factory=AgentEnvironmentOptions,
        description="Environment for every agent of the node; how one node alone receives a credential",
    )
    templates: tuple[str, ...] = Field(
        default=(), description="Create template names applied when the node's hosts and agents are made"
    )
    agents_per_host: PositiveInt = Field(
        default=PositiveInt(1), description="How many agents share one pooled host, on providers that pool"
    )
    max_parallel_launch: PositiveInt = Field(
        default=PositiveInt(4), description="How many of the node's agents or hosts may be created at once"
    )
    max_running_agents: NonNegativeInt = Field(
        default=NonNegativeInt(0), description="How many of the node's agents may run at once; 0 means all"
    )
    agent_timeout_seconds: PositiveFloat = Field(
        default=PositiveFloat(3600.0), description="Wall-clock budget for each of the node's agents, from its creation"
    )


class ExecutionPlan(FrozenModel):
    """Everything about an execution that is not the pipeline: where each node runs, and where the run reads and writes."""

    default_placement: NodePlacement = Field(description="The placement of every node the plan does not name")
    placement_by_node_name: dict[NodeName, NodePlacement] = Field(
        default_factory=dict, description="Placements that override the default, keyed by the pipeline's node names"
    )
    source_dir: Path = Field(description="The operator checkout agents are created from and branches are fetched into")
    output_dir: Path = Field(description="Where extracted archives and the execution manifest are written")
    poll_interval_seconds: PositiveFloat = Field(
        default=PositiveFloat(10.0), description="Seconds between checks for a published outputs archive"
    )
    launch_delay_seconds: NonNegativeFloat = Field(
        default=NonNegativeFloat(0.0),
        description="Pause between consecutive agent creations, to stay under provider rate limits",
    )
    max_concurrent_nodes: NonNegativeInt = Field(
        default=NonNegativeInt(0),
        description="How many nodes may run at once; 0 means every node whose dependencies are met",
    )
    max_running_agents: NonNegativeInt = Field(
        default=NonNegativeInt(0),
        description="How many agents may run at once across the whole execution; 0 means no execution-wide limit",
    )
    is_keeping_hosts: bool = Field(
        default=False, description="Whether hosts made for a node outlive it, for live debugging"
    )

    def placement_for(self, node_name: NodeName) -> NodePlacement:
        return self.placement_by_node_name.get(node_name, self.default_placement)


class Execution(FrozenModel):
    """One performance of a pipeline: its identity, its context, its plan, and everything it has produced so far."""

    pipeline_name: str = Field(description="Prefix of every agent, host and branch name the execution creates")
    execution_name: str = Field(
        description="Distinguishes this execution from every other one of the same pipeline; a UTC timestamp "
        "when minted by the CLI"
    )
    base_commit: str = Field(description="The commit the execution started from; a source node's jobs start here")
    context: dict[str, str] = Field(
        default_factory=dict, description="The pipeline's parameters as supplied, available to every prompt template"
    )
    plan: ExecutionPlan = Field(description="Where each node runs, and where the execution reads and writes")
    node_outcome_by_node_name: dict[NodeName, NodeOutcome] = Field(
        default_factory=dict, description="One outcome per node that has ended"
    )
    stopped_reason: str | None = Field(
        default=None, description="Why the execution halted before running every node, or None"
    )

    def with_node_outcome(self, node_outcome: NodeOutcome) -> "Execution":
        outcomes = {**self.node_outcome_by_node_name, node_outcome.node_name: node_outcome}
        return self.model_copy_update(to_update(self.field_ref().node_outcome_by_node_name, outcomes))

    def with_stopped_reason(self, reason: str) -> "Execution":
        return self.model_copy_update(to_update(self.field_ref().stopped_reason, reason))

    def successful_job_results_of(self, node_name: NodeName) -> tuple[JobResult, ...]:
        """The successful results of one node, or nothing when it has not ended or is not an agent node."""
        outcome = self.node_outcome_by_node_name.get(node_name)
        if outcome is None or not isinstance(outcome.produced, AgentNodeProduct):
            return ()
        return outcome.produced.successful_job_results


class NodeHosts(FrozenModel):
    """The hosts an executor provisioned for one node; opaque to everything but the executor that made it."""

    node_name: NodeName = Field(description="The node the hosts were provisioned for")
    snapshot: SnapshotName | None = Field(description="The snapshot the node's hosts were created from, if any")
    host_count: int = Field(
        description="How many pooled hosts the node's agents are spread over; 0 when none are pooled"
    )


class AgentIdentity(FrozenModel):
    """The names an executor assigns to one job's agent before launching it."""

    agent_name: AgentName = Field(description="The agent's name; also the directory its archive is extracted into")
    branch_name: str = Field(description="The branch the agent commits to and publishes")


class LaunchedAgent(FrozenModel):
    """A running agent the executor is waiting on."""

    node_name: NodeName = Field(description="The node the agent works for")
    job: Job = Field(description="The assignment the agent was launched with")
    agent_name: AgentName = Field(description="The agent's name; also the directory its archive is extracted into")
    agent_handle: str = Field(
        description="The executor's own handle for the agent; the provider's agent id for mngr-backed executors"
    )
    branch_name: str = Field(description="The branch the agent commits to and publishes")
    created_at_monotonic: float = Field(description="time.monotonic() at creation, from which its timeout is measured")


@pure
def evaluate_agent_node(work: AgentWork, job_results: Sequence[JobResult]) -> NodeStatus:
    """Whether the node met both of its thresholds.

    ``min_job_count`` is checked first, because ``required_completion`` cannot
    express "must not be empty": zero successes out of zero jobs satisfies any
    fraction, the way ``all([])`` is true. A fan-out of zero therefore succeeds
    vacuously unless the node asserted it expected to find something.
    """
    if len(job_results) < work.min_job_count:
        return NodeStatus.FAILED
    if not job_results:
        return NodeStatus.SUCCEEDED
    successful_count = sum(1 for job_result in job_results if job_result.is_successful)
    required_count = math.ceil(work.required_completion * len(job_results))
    return NodeStatus.SUCCEEDED if successful_count >= required_count else NodeStatus.FAILED

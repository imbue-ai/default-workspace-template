"""A pipeline executor and bindings backed by nothing, for exercising the scheduler.

The mock records what it was asked to do -- which agents were live together,
which prompts were delivered, what was stopped and released -- so a test can
assert on the loop's behavior without a provider, a host, or a filesystem.
"""

import time
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.primitives import AgentName
from imbue.mngr_mapreduce.execution import AgentIdentity
from imbue.mngr_mapreduce.execution import BranchFetch
from imbue.mngr_mapreduce.execution import Execution
from imbue.mngr_mapreduce.execution import GateResult
from imbue.mngr_mapreduce.execution import GateSubject
from imbue.mngr_mapreduce.execution import Job
from imbue.mngr_mapreduce.execution import JobResult
from imbue.mngr_mapreduce.execution import LaunchedAgent
from imbue.mngr_mapreduce.execution import NodeHosts
from imbue.mngr_mapreduce.execution import OrchestratorOutcome
from imbue.mngr_mapreduce.executor import AbstractPipelineExecutor
from imbue.mngr_mapreduce.executor import AgentLaunchError
from imbue.mngr_mapreduce.interfaces import DiscoveredJobsBindingInterface
from imbue.mngr_mapreduce.interfaces import ExecutionObserverInterface
from imbue.mngr_mapreduce.interfaces import GateBindingInterface
from imbue.mngr_mapreduce.interfaces import OrchestratorNodeBindingInterface
from imbue.mngr_mapreduce.interfaces import PerPartitionBindingInterface
from imbue.mngr_mapreduce.interfaces import PerUpstreamJobBindingInterface
from imbue.mngr_mapreduce.interfaces import SingleJobBindingInterface
from imbue.mngr_mapreduce.pipeline import Gate
from imbue.mngr_mapreduce.pipeline import Node
from imbue.mngr_mapreduce.primitives import NodeName
from imbue.mngr_mapreduce.primitives import PartitionKey
from imbue.mngr_mapreduce.testing import make_job


class MockPipelineExecutor(AbstractPipelineExecutor):
    """An executor whose agents publish after a fixed number of polls and never touch anything real."""

    polls_before_publishing: int = Field(default=2, description="How many poll passes each agent stays in flight")
    never_publishing_slugs: frozenset[str] = Field(
        default=frozenset(), description="Job slugs whose agents never publish, so they time out"
    )
    unlaunchable_slugs: frozenset[str] = Field(
        default=frozenset(), description="Job slugs whose launch raises AgentLaunchError"
    )
    unpullable_slugs: frozenset[str] = Field(
        default=frozenset(), description="Job slugs whose archive cannot be pulled"
    )
    unfetchable_slugs: frozenset[str] = Field(
        default=frozenset(), description="Job slugs that published a branch bundle which fails to apply"
    )
    branchless_slugs: frozenset[str] = Field(
        default=frozenset(), description="Job slugs whose agent committed nothing, so there is no bundle"
    )
    poll_count_by_agent_name: dict[AgentName, int] = Field(default_factory=dict, description="Polls seen per agent")
    prompt_by_agent_name: dict[AgentName, str] = Field(default_factory=dict, description="What each agent was told")
    live_node_names_by_agent_name: dict[AgentName, NodeName] = Field(
        default_factory=dict, description="Which node each live agent belongs to"
    )
    live_node_name_snapshots: list[frozenset[NodeName]] = Field(
        default_factory=list, description="The set of nodes with live agents, sampled whenever that set changes"
    )
    provisioned_node_names: list[NodeName] = Field(default_factory=list, description="Nodes hosts were made for")
    released_node_names: list[NodeName] = Field(default_factory=list, description="Nodes hosts were given back for")
    stopped_agent_names: list[AgentName] = Field(default_factory=list, description="Agents that were stopped")
    max_live_agent_count: int = Field(default=0, description="The most agents that were ever live at once")

    def _snapshot(self) -> None:
        self.live_node_name_snapshots.append(frozenset(self.live_node_names_by_agent_name.values()))
        self.max_live_agent_count = max(self.max_live_agent_count, len(self.live_node_names_by_agent_name))

    def provision_hosts(self, node: Node, job_count: int) -> NodeHosts:
        self.provisioned_node_names.append(node.name)
        return NodeHosts(node_name=node.name, snapshot=None, host_count=job_count)

    def launch_agent(
        self, node: Node, job: Job, prompt: str, identity: AgentIdentity, hosts: NodeHosts, job_idx: int
    ) -> LaunchedAgent:
        if job.slug in self.unlaunchable_slugs:
            raise AgentLaunchError(f"mock refuses to launch '{job.slug}'")
        self.prompt_by_agent_name[identity.agent_name] = prompt
        self.live_node_names_by_agent_name[identity.agent_name] = node.name
        self._snapshot()
        return LaunchedAgent(
            node_name=node.name,
            job=job,
            agent_name=identity.agent_name,
            agent_handle=f"handle-{identity.agent_name}",
            branch_name=identity.branch_name,
            created_at_monotonic=time.monotonic(),
        )

    def is_published(self, agent: LaunchedAgent) -> bool:
        if agent.job.slug in self.never_publishing_slugs:
            return False
        seen = self.poll_count_by_agent_name.get(agent.agent_name, 0) + 1
        self.poll_count_by_agent_name[agent.agent_name] = seen
        return seen >= self.polls_before_publishing

    def pull_outputs(self, agent: LaunchedAgent, destination_dir: Path) -> Path | None:
        if agent.job.slug in self.unpullable_slugs:
            return None
        return destination_dir / str(agent.agent_name)

    def fetch_branch(self, agent: LaunchedAgent, archive_dir: Path) -> BranchFetch:
        if agent.job.slug in self.unfetchable_slugs:
            return BranchFetch.FAILED
        if agent.job.slug in self.branchless_slugs:
            return BranchFetch.NONE_PUBLISHED
        return BranchFetch.APPLIED

    def stop_agent(self, agent: LaunchedAgent) -> None:
        self.stopped_agent_names.append(agent.agent_name)
        self.live_node_names_by_agent_name.pop(agent.agent_name, None)
        self._snapshot()

    def release_hosts(self, hosts: NodeHosts) -> None:
        self.released_node_names.append(hosts.node_name)


class MockDiscoveredJobsBinding(DiscoveredJobsBindingInterface):
    """Returns a fixed list of slugs, which is how a test sets a node's width."""

    slugs: tuple[str, ...] = Field(description="One job per slug")
    variable_name: str = Field(default="item", description="The template variable each job supplies its slug as")

    def discover_jobs(self, execution: Execution, node: Node) -> list[Job]:
        return [make_job(slug, **{self.variable_name: slug}) for slug in self.slugs]


class MockRaisingJobsBinding(DiscoveredJobsBindingInterface):
    """A binding that raises a plain built-in, as third-party plugin code may."""

    def discover_jobs(self, execution: Execution, node: Node) -> list[Job]:
        raise ValueError("mock binding cannot decide its jobs")


class MockSingleJobBinding(SingleJobBindingInterface):
    slug: str = Field(default="only", description="The one job's slug")
    variable_name: str = Field(default="item", description="The template variable the job supplies its slug as")

    def build_job(self, execution: Execution, node: Node) -> Job:
        return make_job(self.slug, **{self.variable_name: self.slug})


class MockPerUpstreamJobBinding(PerUpstreamJobBindingInterface):
    """Builds one job per upstream result, declining any whose slug is listed."""

    declined_slugs: frozenset[str] = Field(default=frozenset(), description="Upstream slugs to produce no job for")
    variable_name: str = Field(default="item", description="The template variable each job supplies its input as")

    def build_job(self, execution: Execution, node: Node, upstream_result: JobResult) -> Job | None:
        if upstream_result.job.slug in self.declined_slugs:
            return None
        return make_job(f"{upstream_result.job.slug}-next", **{self.variable_name: upstream_result.job.slug})


class MockPerPartitionBinding(PerPartitionBindingInterface):
    """Partitions upstream results by a prefix of their slug."""

    prefix_length: int = Field(default=1, description="How many leading characters of the slug name the partition")
    key_variable_name: str = Field(
        default="item", description="The template variable the partition key is supplied as"
    )
    members_variable_name: str = Field(
        default="members", description="The template variable the partition's upstream slugs are supplied as"
    )
    upstream_slugs_by_key: dict[str, tuple[str, ...]] = Field(
        default_factory=dict, description="What each partition's job was handed"
    )

    def partition(self, execution: Execution, node: Node, upstream_result: JobResult) -> PartitionKey:
        return PartitionKey(upstream_result.job.slug[: self.prefix_length])

    def build_job(
        self, execution: Execution, node: Node, key: PartitionKey, upstream_results: tuple[JobResult, ...]
    ) -> Job:
        member_slugs = tuple(result.job.slug for result in upstream_results)
        self.upstream_slugs_by_key[str(key)] = member_slugs
        return make_job(
            f"partition-{key}",
            **{self.key_variable_name: str(key), self.members_variable_name: ", ".join(member_slugs)},
        )


class MockOrchestratorBinding(OrchestratorNodeBindingInterface):
    """Records that it ran, and fails when told to."""

    error_summary: str | None = Field(default=None, description="Set to make the orchestrator node fail")
    run_count: int = Field(default=0, description="How many times the binding ran")

    def run(self, execution: Execution, node: Node) -> OrchestratorOutcome:
        self.run_count += 1
        return OrchestratorOutcome(
            branch_name=None if self.error_summary else f"merged/{node.name}",
            summary=f"ran {node.name}",
            error_summary=self.error_summary,
        )


class MockGateBinding(GateBindingInterface):
    """Passes every job except those whose slug is listed, and raises when told to."""

    failing_slugs: frozenset[str] = Field(default=frozenset(), description="Slugs the gate rejects")
    is_raising: bool = Field(default=False, description="Whether the gate raises instead of rendering a verdict")

    def check(self, gate: Gate, subject: GateSubject) -> GateResult:
        if self.is_raising:
            raise AgentLaunchError("mock gate cannot render a verdict")
        is_passed = subject.job.slug not in self.failing_slugs
        return GateResult(gate_name=str(gate.name), is_passed=is_passed, detail="mock verdict")


class RecordingObserver(ExecutionObserverInterface):
    """Keeps the status of every node at each change, so a test can see the order things settled in."""

    statuses_seen: list[dict[str, str]] = Field(default_factory=list, description="Node name to status, per change")

    def on_execution_changed(self, execution: Execution) -> None:
        self.statuses_seen.append(
            {str(name): str(outcome.status) for name, outcome in execution.node_outcome_by_node_name.items()}
        )


class MockUnusedBinding(MutableModel):
    """A binding of no particular shape, for asserting that a wrong-shaped binding is refused."""

    ...


class MockBranchRecordingGate(GateBindingInterface):
    """Passes everything, and records whether each subject had a branch."""

    seen_is_branch_applied: list[bool] = Field(
        default_factory=list, description="One entry per job the gate was shown, in the order it saw them"
    )

    def check(self, gate: Gate, subject: GateSubject) -> GateResult:
        self.seen_is_branch_applied.append(subject.is_branch_applied)
        return GateResult(gate_name=str(gate.name), is_passed=True, detail="mock verdict")

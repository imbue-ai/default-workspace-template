"""Running a pipeline on real mngr agents.

Fills in the methods ``AbstractPipelineExecutor`` leaves abstract, placing
each node where its execution plan says. A node on the local provider runs on
the operator's own host. A node on a provider that supports snapshots gets a
snapshot and a host pool of its own, taken after the branches it depends on
exist, and its agents are placed as worktrees off the pool's shared clone. A
node on any other provider gets a fresh host per agent. Publishing, pulling and
stopping are the same calls the recipe path makes.
"""

import math
import time
from pathlib import Path

from loguru import logger
from pydantic import Field

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.mngr.api.create import bootstrap_backend_for_host_creation
from imbue.mngr.api.data_types import CreateAgentResult
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.api.rsync import rsync_to_remote
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import require_interactive_agent
from imbue.mngr.interfaces.host import AgentLabelOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import UncommittedChangesMode
from imbue.mngr_mapreduce.agent_stopper import AgentStopper
from imbue.mngr_mapreduce.bundle import BRANCH_BUNDLE_NAME
from imbue.mngr_mapreduce.bundle import apply_branch_bundle
from imbue.mngr_mapreduce.cli import RUN_NAME_LABEL_KEY
from imbue.mngr_mapreduce.cli import disable_modal_initial_snapshot
from imbue.mngr_mapreduce.data_types import LaunchConfig
from imbue.mngr_mapreduce.execution import AgentIdentity
from imbue.mngr_mapreduce.execution import BranchFetch
from imbue.mngr_mapreduce.execution import Execution
from imbue.mngr_mapreduce.execution import Job
from imbue.mngr_mapreduce.execution import LaunchedAgent
from imbue.mngr_mapreduce.execution import NodeHosts
from imbue.mngr_mapreduce.execution import NodePlacement
from imbue.mngr_mapreduce.executor import AbstractPipelineExecutor
from imbue.mngr_mapreduce.executor import AgentLaunchError
from imbue.mngr_mapreduce.launching import REDUCER_INPUTS_DIRNAME
from imbue.mngr_mapreduce.launching import create_agent_for_node
from imbue.mngr_mapreduce.launching import create_hosts_named
from imbue.mngr_mapreduce.launching import create_snapshot_host_named
from imbue.mngr_mapreduce.pipeline import Node
from imbue.mngr_mapreduce.primitives import NodeName
from imbue.mngr_mapreduce.pulling import is_agent_outputs_ready
from imbue.mngr_mapreduce.pulling import pull_agent_outputs


class UnknownPlannedProviderError(MngrError, ValueError):
    """Raised before any launch when an execution plan names a provider mngr cannot resolve."""

    ...


@pure
def is_local_provider(provider_name: ProviderInstanceName) -> bool:
    return str(provider_name).lower() == LOCAL_PROVIDER_NAME


class MngrPipelineExecutor(AbstractPipelineExecutor):
    """An executor that creates real agents through mngr."""

    mngr_ctx: MngrContext = Field(frozen=True, description="The mngr context agents are created through")
    source_host: OnlineHostInterface = Field(
        frozen=True, description="The operator's host, where the source repo lives"
    )
    stopper: AgentStopper = Field(
        frozen=True,
        default_factory=AgentStopper,
        description="Stops agents on background threads so a slow stop never stalls polling",
    )
    host_by_agent_handle: dict[str, OnlineHostInterface] = Field(
        default_factory=dict, description="The host each launched agent lives on, by agent id"
    )
    launch_config_by_node_name: dict[NodeName, LaunchConfig] = Field(
        default_factory=dict,
        description="How each provisioned node's agents are created, snapshot included when pooled",
    )
    pool_by_node_name: dict[NodeName, list[OnlineHostInterface]] = Field(
        default_factory=dict, description="The pooled hosts each node's agents are spread over"
    )
    created_hosts_by_node_name: dict[NodeName, list[OnlineHostInterface]] = Field(
        default_factory=dict, description="Every host made for each node, to destroy when the node ends"
    )

    def execute(self) -> Execution:
        self._assert_planned_providers_are_known()
        for provider_name in self._planned_provider_names():
            disable_modal_initial_snapshot(self.mngr_ctx, str(provider_name))
        with self.stopper:
            return super().execute()

    def provision_hosts(self, node: Node, job_count: int) -> NodeHosts:
        placement = self.execution.plan.placement_for(node.name)
        base_config = self._make_launch_config(placement)
        if is_local_provider(placement.provider):
            self.launch_config_by_node_name[node.name] = base_config
            return NodeHosts(node_name=node.name, snapshot=None, host_count=0)

        bootstrap_backend_for_host_creation(placement.provider, self.mngr_ctx)
        provider = get_provider_instance(placement.provider, self.mngr_ctx)
        if not provider.supports_snapshots:
            self.launch_config_by_node_name[node.name] = base_config
            return NodeHosts(node_name=node.name, snapshot=None, host_count=0)

        # Snapshot the operator checkout as it is now, so the pool's shared clone
        # holds every branch this node's dependencies produced, then spread the
        # node's agents over a pool sized by the placement.
        stem = self._node_name_stem(node.name)
        snapshot_host = create_snapshot_host_named(
            agent_name=AgentName(f"{stem}-snapshotter"),
            host_name=HostName(f"{stem}-snapshotter"),
            branch_name=f"{self.execution.pipeline_name}/{self.execution.execution_name}/{node.name}/snapshotter",
            config=base_config,
            mngr_ctx=self.mngr_ctx,
        )
        pooled_config = base_config.model_copy_update(
            to_update(base_config.field_ref().snapshot, snapshot_host.snapshot)
        )
        host_count = math.ceil(job_count / placement.agents_per_host)
        pool = create_hosts_named(
            host_names=[HostName(f"{stem}-host-{host_idx}") for host_idx in range(host_count)],
            config=pooled_config,
            mngr_ctx=self.mngr_ctx,
            max_parallel=placement.max_parallel_launch,
        )
        self.launch_config_by_node_name[node.name] = pooled_config
        self.pool_by_node_name[node.name] = pool
        self.created_hosts_by_node_name[node.name] = [*pool, snapshot_host.host]
        return NodeHosts(node_name=node.name, snapshot=snapshot_host.snapshot, host_count=len(pool))

    def launch_agent(
        self,
        node: Node,
        job: Job,
        prompt: str,
        identity: AgentIdentity,
        hosts: NodeHosts,
        job_idx: int,
    ) -> LaunchedAgent:
        config = self.launch_config_by_node_name[node.name]
        pool = self.pool_by_node_name.get(node.name, [])
        is_fresh_host = not pool and not is_local_provider(config.provider_name)
        try:
            create_result = self.create_silent_agent(
                node=node,
                job=job,
                identity=identity,
                config=config,
                existing_host=pool[job_idx % len(pool)] if pool else None,
                host_name=HostName(f"{self._node_name_stem(node.name)}-host-{job_idx}") if is_fresh_host else None,
            )
        except (MngrError, OSError, BaseExceptionGroup) as exc:
            raise AgentLaunchError(f"Could not launch '{identity.agent_name}': {exc}") from exc

        # Recorded before the agent is prompted, so a failure between here and the
        # prompt still leaves the agent and its host reachable by the halt path.
        agent_handle = str(create_result.agent.id)
        self.host_by_agent_handle[agent_handle] = create_result.host
        if is_fresh_host:
            self.created_hosts_by_node_name.setdefault(node.name, []).append(create_result.host)

        try:
            if job.inputs_dir is not None:
                self._hand_over_inputs(create_result.host, create_result.agent.work_dir, job.inputs_dir)
            self.deliver_prompt(create_result.agent, prompt)
        except (MngrError, OSError, BaseExceptionGroup) as exc:
            self.stopper.submit(create_result.host, AgentId(agent_handle), create_result.agent.name)
            raise AgentLaunchError(f"Could not prompt '{identity.agent_name}': {exc}") from exc
        return LaunchedAgent(
            node_name=node.name,
            job=job,
            agent_name=create_result.agent.name,
            agent_handle=agent_handle,
            branch_name=identity.branch_name,
            created_at_monotonic=time.monotonic(),
        )

    def create_silent_agent(
        self,
        node: Node,
        job: Job,
        identity: AgentIdentity,
        config: LaunchConfig,
        existing_host: OnlineHostInterface | None,
        host_name: HostName | None,
    ) -> CreateAgentResult:
        """Bring one job's agent into existence without a message.

        Handing the prompt to creation would let a delivery failure raise before there
        is a handle to record, leaving the agent running and unreachable by the halt
        path: a leaked session on the local provider, a leaked host on Modal.
        """
        return create_agent_for_node(
            agent_name=identity.agent_name,
            branch_name=identity.branch_name,
            config=config,
            mngr_ctx=self.mngr_ctx,
            role=str(node.name),
            base_ref=job.base_ref,
            initial_message=None,
            existing_host=existing_host,
            host_name=host_name,
        )

    def deliver_prompt(self, agent: AgentInterface, prompt: str) -> None:
        """Hand one job's prompt to an agent that is already created and recorded."""
        require_interactive_agent(agent).send_message(prompt)

    def is_published(self, agent: LaunchedAgent) -> bool:
        return is_agent_outputs_ready(
            self.mngr_ctx, self._provider_of(agent), self._host_of(agent).id, AgentId(agent.agent_handle)
        )

    def pull_outputs(self, agent: LaunchedAgent, destination_dir: Path) -> Path | None:
        return pull_agent_outputs(
            mngr_ctx=self.mngr_ctx,
            provider_name=self._provider_of(agent),
            host_id=self._host_of(agent).id,
            agent_id=AgentId(agent.agent_handle),
            agent_name=agent.agent_name,
            destination_dir=destination_dir,
        )

    def fetch_branch(self, agent: LaunchedAgent, archive_dir: Path) -> BranchFetch:
        bundle_path = archive_dir / BRANCH_BUNDLE_NAME
        if not bundle_path.exists():
            logger.debug("Agent '{}' published no branch bundle", agent.agent_name)
            return BranchFetch.NONE_PUBLISHED
        is_applied = apply_branch_bundle(
            source_dir=self.execution.plan.source_dir,
            bundle_path=bundle_path,
            branch_name=agent.branch_name,
            agent_name=str(agent.agent_name),
            cg=self.mngr_ctx.concurrency_group,
        )
        return BranchFetch.APPLIED if is_applied else BranchFetch.FAILED

    def stop_agent(self, agent: LaunchedAgent) -> None:
        self.stopper.submit(self._host_of(agent), AgentId(agent.agent_handle), agent.agent_name)

    def release_hosts(self, hosts: NodeHosts) -> None:
        self.pool_by_node_name.pop(hosts.node_name, None)
        created_hosts = self.created_hosts_by_node_name.pop(hosts.node_name, [])
        if self.execution.plan.is_keeping_hosts or not created_hosts:
            return
        provider = get_provider_instance(self.launch_config_by_node_name[hosts.node_name].provider_name, self.mngr_ctx)
        for host in created_hosts:
            try:
                provider.destroy_host(host)
            except (MngrError, OSError) as exc:
                logger.warning("Failed to destroy host '{}' of node '{}': {}", host.id, hosts.node_name, exc)

    def _assert_planned_providers_are_known(self) -> None:
        for provider_name in self._planned_provider_names():
            if is_local_provider(provider_name):
                continue
            try:
                get_provider_instance(provider_name, self.mngr_ctx)
            except MngrError as exc:
                raise UnknownPlannedProviderError(
                    f"Execution plan places a node on provider '{provider_name}', which mngr cannot resolve: {exc}"
                ) from exc

    def _planned_provider_names(self) -> list[ProviderInstanceName]:
        plan = self.execution.plan
        planned = [
            plan.default_placement.provider,
            *(placement.provider for placement in plan.placement_by_node_name.values()),
        ]
        return sorted(set(planned))

    def _make_launch_config(self, placement: NodePlacement) -> LaunchConfig:
        return LaunchConfig(
            source_dir=self.execution.plan.source_dir,
            source_host=self.source_host,
            base_commit=self.execution.base_commit,
            agent_type=placement.agent_type,
            provider_name=placement.provider,
            env_options=placement.env_options,
            reducer_env_options=None,
            label_options=AgentLabelOptions(labels={RUN_NAME_LABEL_KEY: self.execution.execution_name}),
            snapshot=None,
            templates=placement.templates,
            additional_authorized_keys=(),
        )

    def _hand_over_inputs(self, host: OnlineHostInterface, work_dir: Path, inputs_dir: Path) -> None:
        """Copy a job's inputs into the agent's work dir, the way the recipe reducer receives its mapper outputs."""
        rsync_to_remote(
            local_path=f"{inputs_dir}/",
            remote_host=host,
            remote_path=work_dir / REDUCER_INPUTS_DIRNAME,
            extra_args=(),
            uncommitted_changes=UncommittedChangesMode.CLOBBER,
            cg=self.mngr_ctx.concurrency_group,
        )

    def _node_name_stem(self, node_name: NodeName) -> str:
        return f"{self.execution.pipeline_name}-{self.execution.execution_name}-{node_name}"

    def _host_of(self, agent: LaunchedAgent) -> OnlineHostInterface:
        return self.host_by_agent_handle[agent.agent_handle]

    def _provider_of(self, agent: LaunchedAgent) -> ProviderInstanceName:
        return self.launch_config_by_node_name[agent.node_name].provider_name

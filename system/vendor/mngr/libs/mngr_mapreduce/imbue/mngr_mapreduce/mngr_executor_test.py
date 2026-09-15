from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.data_types import CreateAgentResult
from imbue.mngr.cli.testing import create_test_agent_state
from imbue.mngr.config.data_types import EnvVar
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_mapreduce.agent_stopper import AgentStopper
from imbue.mngr_mapreduce.bindings import PipelineBindings
from imbue.mngr_mapreduce.cli import RUN_NAME_LABEL_KEY
from imbue.mngr_mapreduce.execution import AgentIdentity
from imbue.mngr_mapreduce.execution import Job
from imbue.mngr_mapreduce.execution import NodeHosts
from imbue.mngr_mapreduce.execution import NodePlacement
from imbue.mngr_mapreduce.executor import AgentLaunchError
from imbue.mngr_mapreduce.launching import LaunchConfig
from imbue.mngr_mapreduce.mngr_executor import MngrPipelineExecutor
from imbue.mngr_mapreduce.mngr_executor import UnknownPlannedProviderError
from imbue.mngr_mapreduce.mngr_executor import is_local_provider
from imbue.mngr_mapreduce.mock_executor_test import MockDiscoveredJobsBinding
from imbue.mngr_mapreduce.pipeline import Node
from imbue.mngr_mapreduce.pipeline import Pipeline
from imbue.mngr_mapreduce.primitives import NodeName
from imbue.mngr_mapreduce.testing import make_execution
from imbue.mngr_mapreduce.testing import make_execution_plan
from imbue.mngr_mapreduce.testing import make_job
from imbue.mngr_mapreduce.testing import make_node
from imbue.mngr_mapreduce.testing import make_pipeline


@pytest.mark.parametrize(
    "provider,is_expected_local",
    [("local", True), ("LOCAL", True), ("modal", False), ("docker", False), ("local_thing", False)],
)
def test_is_local_provider(provider: str, is_expected_local: bool) -> None:
    assert is_local_provider(ProviderInstanceName(provider)) is is_expected_local


def _pipeline() -> Pipeline:
    return make_pipeline((make_node("map", needs=("seed",), produces=("a",)),))


def _bindings() -> PipelineBindings:
    return PipelineBindings(agent_binding_by_node_name={NodeName("map"): MockDiscoveredJobsBinding(slugs=("one",))})


def _executor(temp_mngr_ctx: MngrContext, local_host: Host) -> MngrPipelineExecutor:
    plan = make_execution_plan()
    with ConcurrencyGroup(name=f"mngr-executor-test-{uuid4().hex}") as cg:
        return MngrPipelineExecutor(
            pipeline=_pipeline(),
            bindings=_bindings(),
            concurrency_group=cg,
            execution=make_execution(plan),
            mngr_ctx=temp_mngr_ctx,
            source_host=local_host,
        )


def test_planned_provider_names_covers_the_default_and_every_override(
    temp_mngr_ctx: MngrContext, local_host: Host
) -> None:
    """Every provider a plan can reach must be resolved before anything launches, not just the default."""
    plan = make_execution_plan()
    override = plan.default_placement.model_copy_update(
        to_update(plan.default_placement.field_ref().provider, ProviderInstanceName("modal"))
    )
    plan = plan.model_copy_update(to_update(plan.field_ref().placement_by_node_name, {NodeName("map"): override}))
    with ConcurrencyGroup(name=f"mngr-executor-test-{uuid4().hex}") as cg:
        executor = MngrPipelineExecutor(
            pipeline=_pipeline(),
            bindings=_bindings(),
            concurrency_group=cg,
            execution=make_execution(plan),
            mngr_ctx=temp_mngr_ctx,
            source_host=local_host,
        )

        assert executor._planned_provider_names() == [
            ProviderInstanceName("local"),
            ProviderInstanceName("modal"),
        ]


def test_execute_refuses_a_plan_naming_a_provider_mngr_cannot_resolve(
    temp_mngr_ctx: MngrContext, local_host: Host
) -> None:
    """A misspelled provider must fail before an agent exists, not halfway through a run."""
    plan = make_execution_plan()
    unknown = plan.default_placement.model_copy_update(
        to_update(plan.default_placement.field_ref().provider, ProviderInstanceName("not_a_provider"))
    )
    plan = plan.model_copy_update(to_update(plan.field_ref().default_placement, unknown))
    with ConcurrencyGroup(name=f"mngr-executor-test-{uuid4().hex}") as cg:
        executor = MngrPipelineExecutor(
            pipeline=_pipeline(),
            bindings=_bindings(),
            concurrency_group=cg,
            execution=make_execution(plan),
            mngr_ctx=temp_mngr_ctx,
            source_host=local_host,
        )

        with pytest.raises(UnknownPlannedProviderError, match="not_a_provider"):
            executor.execute()


def test_launch_config_carries_the_nodes_own_environment_and_the_run_label(
    temp_mngr_ctx: MngrContext, local_host: Host
) -> None:
    """A node's environment is its placement's, which is how one node alone receives a credential."""
    placement = NodePlacement(
        provider=ProviderInstanceName("local"),
        agent_type=make_execution_plan().default_placement.agent_type,
        env_options=AgentEnvironmentOptions(env_vars=(EnvVar(key="ONLY_FOR_THIS_NODE", value="secret"),)),
    )
    executor = _executor(temp_mngr_ctx, local_host)

    config = executor._make_launch_config(placement)

    assert config.env_options.env_vars[0].key == "ONLY_FOR_THIS_NODE"
    assert config.label_options.labels[RUN_NAME_LABEL_KEY] == "20260911000000"
    assert config.reducer_env_options is None


class _RecordingStopper(AgentStopper):
    """Records what was submitted instead of stopping it, so a test can see the cleanup."""

    stopped_agent_names: list[AgentName] = Field(default_factory=list, description="Agents handed to the stopper")

    def submit(self, host: OnlineHostInterface, agent_id: AgentId, agent_name: AgentName) -> None:
        self.stopped_agent_names.append(agent_name)


class _CreatesButCannotPromptExecutor(MngrPipelineExecutor):
    """Creates a real agent state, then fails to deliver its prompt the way mngr did in the field."""

    work_dir: Path = Field(description="Where the created agent's state is put")
    agent_host: Host = Field(description="The concrete host the agent state is created on")

    def create_silent_agent(
        self,
        node: Node,
        job: Job,
        identity: AgentIdentity,
        config: LaunchConfig,
        existing_host: OnlineHostInterface | None,
        host_name: HostName | None,
    ) -> CreateAgentResult:
        agent = create_test_agent_state(self.agent_host, self.work_dir, str(identity.agent_name))
        return CreateAgentResult(agent=agent, host=self.agent_host)

    def deliver_prompt(self, agent: AgentInterface, prompt: str) -> None:
        raise SendMessageError(str(agent.name), "Timeout waiting for message submission evidence")


def test_an_agent_that_cannot_be_prompted_is_stopped_rather_than_left_running(
    temp_mngr_ctx: MngrContext, local_host: Host, tmp_path: Path
) -> None:
    """Message delivery failing after creation must not leak the agent.

    This is what the first real pipeline run hit: mngr timed out waiting for message
    submission evidence, and because the prompt had been handed to creation the
    exception carried no agent handle, so nothing could stop what had been made.
    """
    stopper = _RecordingStopper()
    plan = make_execution_plan()
    with ConcurrencyGroup(name=f"mngr-executor-test-{uuid4().hex}") as cg:
        executor = _CreatesButCannotPromptExecutor(
            pipeline=_pipeline(),
            bindings=_bindings(),
            concurrency_group=cg,
            execution=make_execution(plan),
            mngr_ctx=temp_mngr_ctx,
            source_host=local_host,
            stopper=stopper,
            work_dir=tmp_path,
            agent_host=local_host,
        )
        node = executor.pipeline.nodes[0]
        identity = AgentIdentity(agent_name=AgentName("witness-map-one"), branch_name="witness/map/one")
        executor.launch_config_by_node_name[node.name] = executor._make_launch_config(plan.placement_for(node.name))

        with pytest.raises(AgentLaunchError, match="Could not prompt"):
            executor.launch_agent(
                node=node,
                job=make_job("one"),
                prompt="do the work",
                identity=identity,
                hosts=NodeHosts(node_name=node.name, snapshot=None, host_count=0),
                job_idx=0,
            )

    assert stopper.stopped_agent_names == [AgentName("witness-map-one")]

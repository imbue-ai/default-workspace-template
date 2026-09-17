"""Integration tests for launching map-reduce agents on the local provider."""

from pathlib import Path

import pytest

from imbue.mngr.api.providers import get_local_host
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.interfaces.host import AgentLabelOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_mapreduce.cli import RUN_NAME_LABEL_KEY
from imbue.mngr_mapreduce.data_types import LaunchConfig
from imbue.mngr_mapreduce.data_types import MapReduceContext
from imbue.mngr_mapreduce.data_types import MapReduceTask
from imbue.mngr_mapreduce.launching import ROLE_LABEL_KEY
from imbue.mngr_mapreduce.launching import TASK_ID_LABEL_KEY
from imbue.mngr_mapreduce.launching import launch_all_mappers
from imbue.mngr_mapreduce.launching import stop_agent_on_host
from imbue.mngr_mapreduce.mock_recipe_test import RecordingRecipe
from imbue.mngr_mapreduce.utils import get_base_commit

# A TMR task id is a pytest node id, so the label value has to survive
# slashes, colons and brackets on its way through agent creation.
_PYTEST_NODE_ID_TASK_ID = "libs/mngr/imbue/mngr/api/create_test.py::test_create_agent[case/one]"

# An agent type that just idles, so the launch exercises the real create path
# without needing a real coding agent on the box.
_PROBE_AGENT_TYPE = AgentTypeName("mapreduce-launch-probe")
_PROBE_COMMAND = CommandString("sleep 51937")


@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_launched_mapper_carries_its_task_id_label(
    temp_mngr_ctx: MngrContext,
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    """The reintegrate flow keys a rediscovered run's mappers by this label.

    Asserts the whole launch path -- not just the options builder -- puts the
    task id on the agent.
    """
    temp_mngr_ctx.config.agent_types[_PROBE_AGENT_TYPE] = AgentTypeConfig(
        parent_type=AgentTypeName("command"), command=_PROBE_COMMAND
    )
    task = MapReduceTask(id=_PYTEST_NODE_ID_TASK_ID, display_id="task-id-label-probe")
    recipe = RecordingRecipe()
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    ctx = MapReduceContext(
        mngr_ctx=temp_mngr_ctx,
        source_dir=temp_git_repo,
        run_name="20260101000000",
        output_dir=output_dir,
        output_opts=OutputOptions(output_format=OutputFormat.HUMAN),
    )
    config = LaunchConfig(
        source_dir=temp_git_repo,
        source_host=get_local_host(temp_mngr_ctx),
        base_commit=get_base_commit(temp_git_repo, temp_mngr_ctx.concurrency_group),
        agent_type=_PROBE_AGENT_TYPE,
        provider_name=ProviderInstanceName(LOCAL_PROVIDER_NAME),
        label_options=AgentLabelOptions(labels={RUN_NAME_LABEL_KEY: ctx.run_name}),
    )

    agent_infos, agent_hosts, _ = launch_all_mappers(
        recipe=recipe,
        ctx=ctx,
        tasks=[task],
        config=config,
        mngr_ctx=temp_mngr_ctx,
        launch_failures=[],
        run_name=ctx.run_name,
        launch_delay_seconds=0.0,
    )

    assert len(agent_infos) == 1
    info = agent_infos[0]
    host = agent_hosts[str(info.agent_id)]
    try:
        provider = get_provider_instance(ProviderInstanceName(LOCAL_PROVIDER_NAME), temp_mngr_ctx)
        online_host = provider.get_host(host.id)
        assert isinstance(online_host, OnlineHostInterface)
        agent = next(candidate for candidate in online_host.get_agents() if candidate.id == info.agent_id)
        labels = agent.get_labels()
    finally:
        stop_agent_on_host(host, info.agent_id, info.agent_name)

    assert labels[TASK_ID_LABEL_KEY] == _PYTEST_NODE_ID_TASK_ID
    assert labels[ROLE_LABEL_KEY] == "MAPPER"
    assert labels[RUN_NAME_LABEL_KEY] == ctx.run_name

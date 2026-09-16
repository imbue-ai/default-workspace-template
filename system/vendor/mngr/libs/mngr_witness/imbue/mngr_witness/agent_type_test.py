from pathlib import Path

from imbue.mngr.config.agent_config_registry import resolve_agent_type
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr_witness.agent_type import WITNESS_CLAUDE_AGENT_TYPE_NAME
from imbue.mngr_witness.agent_type import WitnessClaudeAgent
from imbue.mngr_witness.agent_type import WitnessClaudeAgentConfig
from imbue.mngr_witness.agent_type import is_unattended
from imbue.mngr_witness.agent_type import warn_about_attended_agent_types
from imbue.mngr_witness.plan import make_execution_plan_from_flags
from imbue.mngr_witness.plan import parse_node_provider_flag


def _plan(tmp_path: Path, agent_type: str):
    return make_execution_plan_from_flags(
        provider="local",
        agent_type=agent_type,
        env=(),
        templates=(),
        node_overrides=[parse_node_provider_flag("reduce=local")],
        max_running_agents=None,
        agent_timeout_seconds=60.0,
        source_dir=tmp_path,
        output_dir=tmp_path / "out",
        is_keeping_hosts=False,
    )


def test_the_witness_claude_type_is_a_plugin_type_that_never_pauses(temp_mngr_ctx: MngrContext) -> None:
    resolved = resolve_agent_type(WITNESS_CLAUDE_AGENT_TYPE_NAME, temp_mngr_ctx.config)

    assert resolved.agent_class is WitnessClaudeAgent
    assert isinstance(resolved.agent_config, WitnessClaudeAgentConfig)
    assert is_unattended(resolved.agent_config)
    assert "--dangerously-skip-permissions" in resolved.agent_config.cli_args


def test_the_stock_claude_type_is_not_unattended_and_the_plan_warns(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    assert not is_unattended(resolve_agent_type(AgentTypeName("claude"), temp_mngr_ctx.config).agent_config)

    assert warn_about_attended_agent_types(temp_mngr_ctx, _plan(tmp_path, "claude")) == [AgentTypeName("claude")]
    assert warn_about_attended_agent_types(temp_mngr_ctx, _plan(tmp_path, "witness-claude")) == []


def test_a_type_without_the_dialog_switches_counts_as_attended() -> None:
    assert not is_unattended(AgentTypeConfig(cli_args=("--dangerously-skip-permissions",)))

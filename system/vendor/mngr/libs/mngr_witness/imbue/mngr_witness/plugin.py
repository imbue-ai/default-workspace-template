from collections.abc import Sequence

import click

from imbue.mngr import hookimpl
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr_witness.agent_type import WITNESS_CLAUDE_AGENT_TYPE_NAME
from imbue.mngr_witness.agent_type import WitnessClaudeAgent
from imbue.mngr_witness.agent_type import WitnessClaudeAgentConfig
from imbue.mngr_witness.cli import witness


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the witness command with mngr."""
    return [witness]


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the unattended claude type witness agents launch as."""
    return (str(WITNESS_CLAUDE_AGENT_TYPE_NAME), WitnessClaudeAgent, WitnessClaudeAgentConfig)

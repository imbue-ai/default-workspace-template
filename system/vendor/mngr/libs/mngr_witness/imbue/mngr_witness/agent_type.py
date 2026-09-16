"""The agent type witness agents launch as: claude with every permission prompt and startup dialog switched off.

A pipeline agent must never pause for approval, and the operator must not have
to edit their own config to make that so. Registering the type as a plugin
type, rather than deriving one per run, keeps it known to every mngr process,
so ``mngr list`` and ``mngr destroy`` resolve a witness agent after the run.
"""

from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.pure import pure
from imbue.mngr.config.agent_config_registry import resolve_agent_type
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr_claude.plugin import ClaudeAgent
from imbue.mngr_claude.plugin import ClaudeAgentConfig
from imbue.mngr_mapreduce.execution import ExecutionPlan

WITNESS_CLAUDE_AGENT_TYPE_NAME: Final[AgentTypeName] = AgentTypeName("witness-claude")

_SKIP_PERMISSIONS_FLAG: Final[str] = "--dangerously-skip-permissions"
_AUTO_DISMISS_FIELD: Final[str] = "auto_dismiss_dialogs_at_startup"
_AUTO_ALLOW_FIELD: Final[str] = "auto_allow_permissions"


class WitnessClaudeAgentConfig(ClaudeAgentConfig):
    """Claude's config with the unattended switches on by default; an operator's overrides still apply."""

    cli_args: tuple[str, ...] = Field(
        default=(_SKIP_PERMISSIONS_FLAG,),
        description="Additional CLI arguments to pass to the agent; the permission bypass is on by default",
    )
    auto_dismiss_dialogs_at_startup: bool = Field(
        default=True, description="Dismiss every startup dialog, so the agent never waits for the operator"
    )
    auto_allow_permissions: bool = Field(
        default=True, description="Auto-allow every permission dialog, so the agent never waits for the operator"
    )


class WitnessClaudeAgent(ClaudeAgent):
    """The claude agent under the witness configuration; the class is claude's, the defaults are not."""

    agent_config: WitnessClaudeAgentConfig = Field(frozen=True, repr=False, description="Agent type config")


@pure
def is_unattended(config: AgentTypeConfig) -> bool:
    """Whether the config keeps the agent from pausing: it knows the dialog switches and has them all on."""
    values = config.model_dump()
    return (
        values.get(_AUTO_DISMISS_FIELD) is True
        and values.get(_AUTO_ALLOW_FIELD) is True
        and _SKIP_PERMISSIONS_FLAG in config.cli_args
    )


def warn_about_attended_agent_types(mngr_ctx: MngrContext, plan: ExecutionPlan) -> list[AgentTypeName]:
    """Warn for every agent type the plan launches that may pause for approval, and return them."""
    attended: list[AgentTypeName] = []
    placements = [plan.default_placement, *plan.placement_by_node_name.values()]
    for requested in sorted({placement.agent_type for placement in placements}):
        if is_unattended(resolve_agent_type(requested, mngr_ctx.config).agent_config):
            continue
        attended.append(requested)
        logger.warning(
            "Agent type '{}' may pause for a permission or startup dialog; '{}' never does",
            requested,
            WITNESS_CLAUDE_AGENT_TYPE_NAME,
        )
    return attended

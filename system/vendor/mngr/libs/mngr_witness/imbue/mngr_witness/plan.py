from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.primitives import NonNegativeFloat
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveFloat
from imbue.imbue_common.primitives import PositiveInt
from imbue.imbue_common.pure import pure
from imbue.mngr.cli.env_utils import resolve_env_vars
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_mapreduce.execution import ExecutionPlan
from imbue.mngr_mapreduce.execution import NodePlacement
from imbue.mngr_mapreduce.primitives import NodeName

DEFAULT_AGENT_TIMEOUT_SECONDS: Final[float] = 3600.0
DEFAULT_POLL_INTERVAL_SECONDS: Final[float] = 60.0
DEFAULT_LAUNCH_DELAY_SECONDS: Final[float] = 2.0
DEFAULT_MAX_PARALLEL_LAUNCH: Final[int] = 10
DEFAULT_AGENTS_PER_HOST: Final[int] = 4
DEFAULT_LOCAL_MAX_RUNNING_AGENTS: Final[int] = 6


class PlanOptionError(MngrError, ValueError):
    """Raised when a placement flag cannot be turned into an execution plan."""

    ...


class NodeOverride(FrozenModel):
    """One ``--node-provider`` or ``--node-env`` flag, parsed: which node, and what changes for it."""

    node_name: NodeName = Field(description="The node the override applies to")
    provider: ProviderInstanceName | None = Field(description="The provider to place the node on, if given")
    env_vars: tuple[str, ...] = Field(description="KEY=VALUE pairs for the node's agents, if given")


@pure
def parse_node_provider_flag(value: str) -> NodeOverride:
    """``reduce=local`` into an override placing the reduce node on the local provider."""
    node_name, separator, provider = value.partition("=")
    if not separator or not node_name or not provider:
        raise PlanOptionError(f"--node-provider expects <node>=<provider>, got {value!r}")
    return NodeOverride(node_name=NodeName(node_name), provider=ProviderInstanceName(provider), env_vars=())


@pure
def parse_node_env_flag(value: str) -> NodeOverride:
    """``reduce:GH_TOKEN=abc`` into an override giving the reduce node's agents that variable."""
    node_name, separator, assignment = value.partition(":")
    if not separator or not node_name or "=" not in assignment:
        raise PlanOptionError(f"--node-env expects <node>:KEY=VALUE, got {value!r}")
    return NodeOverride(node_name=NodeName(node_name), provider=None, env_vars=(assignment,))


@pure
def default_max_running_agents(provider: ProviderInstanceName) -> int:
    """Six agents at once on a laptop, unbounded elsewhere; the spec's default for the local provider."""
    return DEFAULT_LOCAL_MAX_RUNNING_AGENTS if str(provider).lower() == LOCAL_PROVIDER_NAME else 0


def make_execution_plan_from_flags(
    provider: str,
    agent_type: str,
    env: Sequence[str],
    templates: Sequence[str],
    node_overrides: Sequence[NodeOverride],
    max_running_agents: int | None,
    agent_timeout_seconds: float,
    source_dir: Path,
    output_dir: Path,
    is_keeping_hosts: bool,
) -> ExecutionPlan:
    """Build the plan the CLI flags describe: one default placement, and an override per node the flags name."""
    default_provider = ProviderInstanceName(provider)
    default_placement = NodePlacement(
        provider=default_provider,
        agent_type=AgentTypeName(agent_type),
        env_options=AgentEnvironmentOptions(env_vars=resolve_env_vars((), tuple(env))),
        templates=tuple(templates),
        agents_per_host=PositiveInt(DEFAULT_AGENTS_PER_HOST),
        max_parallel_launch=PositiveInt(DEFAULT_MAX_PARALLEL_LAUNCH),
        max_running_agents=NonNegativeInt(
            max_running_agents if max_running_agents is not None else default_max_running_agents(default_provider)
        ),
        agent_timeout_seconds=PositiveFloat(agent_timeout_seconds),
    )
    placement_by_node_name: dict[NodeName, NodePlacement] = {}
    for override in node_overrides:
        current = placement_by_node_name.get(override.node_name, default_placement)
        placement_by_node_name[override.node_name] = _apply_override(current, override, max_running_agents)
    return ExecutionPlan(
        default_placement=default_placement,
        placement_by_node_name=placement_by_node_name,
        source_dir=source_dir,
        output_dir=output_dir,
        poll_interval_seconds=PositiveFloat(DEFAULT_POLL_INTERVAL_SECONDS),
        launch_delay_seconds=NonNegativeFloat(DEFAULT_LAUNCH_DELAY_SECONDS),
        is_keeping_hosts=is_keeping_hosts,
    )


@pure
def _with_env_vars(base: AgentEnvironmentOptions, assignments: tuple[str, ...]) -> AgentEnvironmentOptions:
    """The base environment plus the given KEY=VALUE assignments, later ones winning at launch time."""
    return base.model_copy_update(
        to_update(base.field_ref().env_vars, (*base.env_vars, *resolve_env_vars((), assignments)))
    )


@pure
def _apply_override(placement: NodePlacement, override: NodeOverride, max_running_agents: int | None) -> NodePlacement:
    provider = override.provider if override.provider is not None else placement.provider
    env_options = (
        _with_env_vars(placement.env_options, override.env_vars) if override.env_vars else placement.env_options
    )
    running_cap = max_running_agents if max_running_agents is not None else default_max_running_agents(provider)
    return NodePlacement(
        provider=provider,
        agent_type=placement.agent_type,
        env_options=env_options,
        templates=placement.templates,
        agents_per_host=placement.agents_per_host,
        max_parallel_launch=placement.max_parallel_launch,
        max_running_agents=NonNegativeInt(running_cap),
        agent_timeout_seconds=placement.agent_timeout_seconds,
    )

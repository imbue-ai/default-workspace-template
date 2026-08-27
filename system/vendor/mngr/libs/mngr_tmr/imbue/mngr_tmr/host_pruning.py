from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr_mapreduce.cli import RUN_NAME_LABEL_KEY


class MapReduceRunHosts(FrozenModel):
    """The hosts one map-reduce run left behind, as discovered from the agents living on them."""

    variant: str = Field(description="Name prefix shared by the run's hosts and agents, e.g. tmr-mngr")
    run_name: str = Field(description="Run name the framework stamped on every agent of the run")
    host_names: tuple[str, ...] = Field(description="Names of the hosts the run's agents live on")
    agent_count: int = Field(description="Number of the run's agents that were discovered")
    latest_agent_create_time: datetime = Field(description="When the most recent of those agents was created")


@pure
def _variant_of_host(host_name: str, run_name: str) -> str:
    """The variant prefix of a map-reduce host name, which is ``<variant>-<run>-<role>``."""
    variant, separator, _role = host_name.partition(f"-{run_name}-")
    if not separator:
        return ""
    return variant


@pure
def group_agents_by_run(agents: Sequence[AgentDetails]) -> tuple[MapReduceRunHosts, ...]:
    """Group the agents that carry a run-name label into one entry per (variant, run)."""

    # Collect each run's agents, keyed by the variant its hosts are named for so
    # that two suites running on their own schedules are pruned independently.
    agents_by_run: dict[tuple[str, str], list[AgentDetails]] = defaultdict(list)
    for agent in agents:
        run_name = agent.labels.get(RUN_NAME_LABEL_KEY)
        if run_name is None:
            continue
        agents_by_run[(_variant_of_host(agent.host.name, run_name), run_name)].append(agent)

    return tuple(
        MapReduceRunHosts(
            variant=variant,
            run_name=run_name,
            host_names=tuple(sorted({agent.host.name for agent in run_agents})),
            agent_count=len(run_agents),
            latest_agent_create_time=max(agent.create_time for agent in run_agents),
        )
        for (variant, run_name), run_agents in sorted(agents_by_run.items())
    )


@pure
def select_prunable_run_names(
    runs: Sequence[MapReduceRunHosts],
    now: datetime,
    kept_run_count_per_variant: int,
    minimum_prunable_age: timedelta,
) -> tuple[str, ...]:
    """Choose which runs' hosts may be destroyed, keeping each variant's most recent runs."""

    # Keep each variant's newest runs, so their hosts stay reachable for debugging,
    # plus every run too young to be sure it has finished.
    runs_by_variant: dict[str, list[MapReduceRunHosts]] = defaultdict(list)
    for run in runs:
        runs_by_variant[run.variant].append(run)
    kept_run_names: set[str] = set()
    for variant_runs in runs_by_variant.values():
        newest_first = sorted(variant_runs, key=lambda run: run.latest_agent_create_time, reverse=True)
        for position, run in enumerate(newest_first):
            is_kept = (
                position < kept_run_count_per_variant or now - run.latest_agent_create_time < minimum_prunable_age
            )
            if is_kept:
                kept_run_names.add(run.run_name)

    # Pruning selects agents by run name alone, so a name two variants happen to
    # share can only be pruned once neither variant keeps it.
    return tuple(sorted({run.run_name for run in runs} - kept_run_names))

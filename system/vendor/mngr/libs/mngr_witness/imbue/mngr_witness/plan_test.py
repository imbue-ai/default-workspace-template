from pathlib import Path

import pytest

from imbue.mngr_mapreduce.execution import ExecutionPlan
from imbue.mngr_mapreduce.primitives import NodeName
from imbue.mngr_witness.plan import DEFAULT_LOCAL_MAX_RUNNING_AGENTS
from imbue.mngr_witness.plan import NodeOverride
from imbue.mngr_witness.plan import PlanOptionError
from imbue.mngr_witness.plan import make_execution_plan_from_flags
from imbue.mngr_witness.plan import parse_node_env_flag
from imbue.mngr_witness.plan import parse_node_provider_flag


def _plan_from_flags(
    tmp_path: Path, provider: str, overrides: list[NodeOverride], max_running_agents: int | None = None
) -> ExecutionPlan:
    return make_execution_plan_from_flags(
        provider=provider,
        agent_type="claude",
        env=("SHARED=1",),
        templates=("modal-tmr",),
        node_overrides=overrides,
        max_running_agents=max_running_agents,
        agent_timeout_seconds=1800.0,
        source_dir=tmp_path,
        output_dir=tmp_path / "out",
        is_keeping_hosts=False,
    )


def test_flags_build_a_plan_with_one_default_placement(tmp_path: Path) -> None:
    plan = _plan_from_flags(tmp_path, "local", [])

    assert str(plan.default_placement.provider) == "local"
    assert plan.default_placement.max_running_agents == DEFAULT_LOCAL_MAX_RUNNING_AGENTS
    assert [var.key for var in plan.default_placement.env_options.env_vars] == ["SHARED"]
    assert plan.default_placement.templates == ("modal-tmr",)
    assert plan.placement_by_node_name == {}
    assert plan.output_dir == tmp_path / "out"


def test_node_overrides_change_only_the_named_stage(tmp_path: Path) -> None:
    overrides = [parse_node_provider_flag("reduce=local"), parse_node_env_flag("reduce:GH_TOKEN=secret")]

    plan = _plan_from_flags(tmp_path, "modal", overrides)

    assert str(plan.placement_for(NodeName("map")).provider) == "modal"
    assert plan.placement_for(NodeName("map")).max_running_agents == 0
    reduce = plan.placement_for(NodeName("reduce"))
    assert str(reduce.provider) == "local"
    assert reduce.max_running_agents == DEFAULT_LOCAL_MAX_RUNNING_AGENTS
    assert {var.key: var.value for var in reduce.env_options.env_vars} == {"SHARED": "1", "GH_TOKEN": "secret"}
    assert [var.key for var in plan.placement_for(NodeName("map")).env_options.env_vars] == ["SHARED"]


def test_an_explicit_running_cap_applies_everywhere(tmp_path: Path) -> None:
    plan = _plan_from_flags(tmp_path, "local", [parse_node_provider_flag("map=modal")], max_running_agents=2)

    assert plan.default_placement.max_running_agents == 2
    assert plan.placement_for(NodeName("map")).max_running_agents == 2


@pytest.mark.parametrize("value", ["reduce", "=local", "reduce=", "reduce:local"])
def test_malformed_node_provider_flags_are_rejected(value: str) -> None:
    with pytest.raises(PlanOptionError, match="--node-provider expects"):
        parse_node_provider_flag(value)


@pytest.mark.parametrize("value", ["reduce", "reduce:", ":GH_TOKEN=x", "reduce:GH_TOKEN"])
def test_malformed_node_env_flags_are_rejected(value: str) -> None:
    with pytest.raises(PlanOptionError, match="--node-env expects"):
        parse_node_env_flag(value)

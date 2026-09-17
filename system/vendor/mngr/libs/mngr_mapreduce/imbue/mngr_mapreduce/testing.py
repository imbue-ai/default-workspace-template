"""Factory helpers for building pipelines in tests.

Every helper fills in the parts a given test does not care about, so a test
body shows only the structure it is actually exercising.
"""

from pathlib import Path
from typing import assert_never

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import NonNegativeInt as _NonNegativeInt
from imbue.imbue_common.primitives import PositiveFloat
from imbue.imbue_common.primitives import UnitFloat
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_mapreduce.execution import AgentNodeProduct
from imbue.mngr_mapreduce.execution import Execution
from imbue.mngr_mapreduce.execution import ExecutionPlan
from imbue.mngr_mapreduce.execution import GateResult
from imbue.mngr_mapreduce.execution import Job
from imbue.mngr_mapreduce.execution import JobResult
from imbue.mngr_mapreduce.execution import NodeOutcome
from imbue.mngr_mapreduce.execution import NodePlacement
from imbue.mngr_mapreduce.pipeline import AgentWork
from imbue.mngr_mapreduce.pipeline import Artifact
from imbue.mngr_mapreduce.pipeline import DiscoveredFanout
from imbue.mngr_mapreduce.pipeline import Fanout
from imbue.mngr_mapreduce.pipeline import FanoutKind
from imbue.mngr_mapreduce.pipeline import Gate
from imbue.mngr_mapreduce.pipeline import Node
from imbue.mngr_mapreduce.pipeline import OrchestratorWork
from imbue.mngr_mapreduce.pipeline import Parameter
from imbue.mngr_mapreduce.pipeline import PerPartitionFanout
from imbue.mngr_mapreduce.pipeline import PerUpstreamJobFanout
from imbue.mngr_mapreduce.pipeline import Pipeline
from imbue.mngr_mapreduce.pipeline import SingleFanout
from imbue.mngr_mapreduce.primitives import ArtifactName
from imbue.mngr_mapreduce.primitives import NodeName


def make_artifact(name: str) -> Artifact:
    return Artifact(name=ArtifactName(name), description=f"the {name}")


def make_gate(name: str) -> Gate:
    return Gate(name=NonEmptyStr(name), description=f"checks {name}")


def make_parameter(name: str, default: str | None = None) -> Parameter:
    return Parameter(name=NonEmptyStr(name), description=f"the {name}", default=default)


def _make_fanout(kind: FanoutKind, over: str | None, needs: tuple[str, ...]) -> Fanout:
    """The fan-out of the given kind, defaulting an upstream one to the first need it was given."""
    source = ArtifactName(over if over is not None else (needs[0] if needs else "seed"))
    match kind:
        case FanoutKind.PER_UPSTREAM_JOB:
            return PerUpstreamJobFanout(over=source, description="one agent")
        case FanoutKind.PER_PARTITION:
            return PerPartitionFanout(over=source, description="one agent")
        case FanoutKind.SINGLE:
            return SingleFanout(description="one agent")
        case FanoutKind.DISCOVERED:
            return DiscoveredFanout(description="one agent")
        case _ as unreachable:
            assert_never(unreachable)


def make_node(
    name: str,
    needs: tuple[str, ...] = (),
    produces: tuple[str, ...] = (),
    kind: FanoutKind = FanoutKind.DISCOVERED,
    over: str | None = None,
    is_orchestrator: bool = False,
    prompt_template: str | None = None,
    job_variables: tuple[str, ...] = (),
    gates: tuple[str, ...] = (),
    required_completion: float = 1.0,
    min_job_count: int = 0,
) -> Node:
    """A node with everything the caller did not specify filled in plausibly."""
    work: AgentWork | OrchestratorWork = (
        OrchestratorWork()
        if is_orchestrator
        else AgentWork(
            fanout=_make_fanout(kind, over, needs),
            prompt_template=NonEmptyStr(prompt_template if prompt_template else f"do the {name} work"),
            job_variables=tuple(NonEmptyStr(variable) for variable in job_variables),
            gates=tuple(make_gate(gate) for gate in gates),
            required_completion=UnitFloat(required_completion),
            min_job_count=NonNegativeInt(min_job_count),
        )
    )
    return Node(
        name=NodeName(name),
        summary=f"the {name} node",
        needs=tuple(ArtifactName(need) for need in needs),
        produces=tuple(make_artifact(product) for product in produces),
        work=work,
    )


def job_results_of(outcome: NodeOutcome) -> tuple[JobResult, ...]:
    """The job results of an agent node's outcome, for tests that assert on what its jobs produced."""
    produced = outcome.produced
    assert isinstance(produced, AgentNodeProduct), f"Node '{outcome.node_name}' is not an agent node"
    return produced.job_results


def make_agent_work(
    name: str = "work",
    kind: FanoutKind = FanoutKind.DISCOVERED,
    gates: tuple[str, ...] = (),
    required_completion: float = 1.0,
    min_job_count: int = 0,
) -> AgentWork:
    """The agent work of a plausible node, for the helpers that judge work rather than nodes."""
    return AgentWork(
        fanout=_make_fanout(kind, None, ()),
        prompt_template=NonEmptyStr(f"do the {name} work"),
        job_variables=(),
        gates=tuple(make_gate(gate) for gate in gates),
        required_completion=UnitFloat(required_completion),
        min_job_count=NonNegativeInt(min_job_count),
    )


def make_pipeline(
    nodes: tuple[Node, ...],
    inputs: tuple[str, ...] = ("seed",),
    outputs: tuple[Artifact, ...] = (),
    parameters: tuple[Parameter, ...] = (),
    template_by_name: dict[str, str] | None = None,
    name: str = "p",
) -> Pipeline:
    return Pipeline(
        name=name,
        parameters=parameters,
        template_by_name=dict(template_by_name) if template_by_name is not None else {},
        inputs=tuple(make_artifact(seed) for seed in inputs),
        nodes=nodes,
        outputs=outputs,
    )


def make_job(slug: str, base_ref: str = "HEAD", **template_variables: str) -> Job:
    return Job(slug=slug, base_ref=base_ref, template_variables=dict(template_variables))


def make_job_result(
    slug: str,
    is_successful: bool = True,
    is_published: bool = True,
    error_summary: str | None = None,
    failed_gate: str | None = None,
) -> JobResult:
    """A job result shaped to succeed or not, by whichever mechanism the caller names.

    ``is_successful=False`` with no other argument produces the commonest case,
    an agent that never published.
    """
    if not is_successful and is_published and error_summary is None and failed_gate is None:
        is_published = False
    gate_results = (GateResult(gate_name=failed_gate, is_passed=False, detail="did not pass"),) if failed_gate else ()
    return JobResult(
        job=make_job(slug),
        agent_name=AgentName(f"agent-{slug}"),
        branch_name=f"branch/{slug}",
        archive_dir=Path(f"/tmp/{slug}") if is_published else None,
        is_branch_applied=is_published,
        error_summary=error_summary,
        gate_results=gate_results,
    )


def make_execution_plan(
    max_concurrent_nodes: int = 0,
    max_running_agents: int = 0,
    node_max_running_agents: int = 0,
    agent_timeout_seconds: float = 3600.0,
) -> ExecutionPlan:
    """A plan that polls fast enough for tests and writes nowhere real."""
    return ExecutionPlan(
        default_placement=NodePlacement(
            provider=ProviderInstanceName("local"),
            agent_type=AgentTypeName("claude"),
            max_running_agents=_NonNegativeInt(node_max_running_agents),
            agent_timeout_seconds=PositiveFloat(agent_timeout_seconds),
        ),
        source_dir=Path("/tmp/source"),
        output_dir=Path("/tmp/output"),
        poll_interval_seconds=PositiveFloat(0.001),
        max_concurrent_nodes=_NonNegativeInt(max_concurrent_nodes),
        max_running_agents=_NonNegativeInt(max_running_agents),
    )


def make_execution(plan: ExecutionPlan | None = None, **context: str) -> Execution:
    return Execution(
        pipeline_name="p",
        execution_name="20260911000000",
        base_commit="abc123",
        context=dict(context),
        plan=plan if plan is not None else make_execution_plan(),
    )

"""Attaching behavior to a pipeline's names, and refusing a pipeline that is missing any.

A pipeline is data and says nothing about how its work is done. ``PipelineBindings``
is the other half: what each node's jobs are, what each orchestrator node does,
and what each gate checks. A pipeline whose names are not all bound, or whose
binding does not match a node's fan-out shape, is refused before any agent
launches.
"""

from typing import assert_never

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr_mapreduce.interfaces import AgentNodeBindingInterface
from imbue.mngr_mapreduce.interfaces import DiscoveredJobsBindingInterface
from imbue.mngr_mapreduce.interfaces import ExecutionObserverInterface
from imbue.mngr_mapreduce.interfaces import GateBindingInterface
from imbue.mngr_mapreduce.interfaces import OrchestratorNodeBindingInterface
from imbue.mngr_mapreduce.interfaces import PerPartitionBindingInterface
from imbue.mngr_mapreduce.interfaces import PerUpstreamJobBindingInterface
from imbue.mngr_mapreduce.interfaces import SingleJobBindingInterface
from imbue.mngr_mapreduce.pipeline import AgentWork
from imbue.mngr_mapreduce.pipeline import FanoutKind
from imbue.mngr_mapreduce.pipeline import Node
from imbue.mngr_mapreduce.pipeline import OrchestratorWork
from imbue.mngr_mapreduce.pipeline import Pipeline
from imbue.mngr_mapreduce.primitives import NodeName

_BINDING_TYPE_BY_FANOUT_KIND: dict[FanoutKind, type] = {
    FanoutKind.SINGLE: SingleJobBindingInterface,
    FanoutKind.PER_UPSTREAM_JOB: PerUpstreamJobBindingInterface,
    FanoutKind.PER_PARTITION: PerPartitionBindingInterface,
    FanoutKind.DISCOVERED: DiscoveredJobsBindingInterface,
}


class UnboundPipelineError(MngrError, ValueError):
    """Raised when a pipeline names a node or gate that its bindings do not implement, or binds one wrongly."""

    ...


class PipelineBindings(FrozenModel):
    """The behavior attached to a pipeline's names."""

    agent_binding_by_node_name: dict[NodeName, AgentNodeBindingInterface] = Field(
        default_factory=dict, description="How each agent node decides its jobs"
    )
    orchestrator_binding_by_node_name: dict[NodeName, OrchestratorNodeBindingInterface] = Field(
        default_factory=dict, description="What each orchestrator node does"
    )
    gate_binding_by_gate_name: dict[str, GateBindingInterface] = Field(
        default_factory=dict, description="What each gate checks, keyed by the gate's name in the pipeline"
    )
    observers: tuple[ExecutionObserverInterface, ...] = Field(
        default=(), description="Told about the execution after every change"
    )


@pure
def _find_agent_binding_problem(node: Node, work: AgentWork, binding: object) -> str | None:
    """Why the node's binding is unusable, or None when it is the shape the node's fan-out needs."""
    if binding is None:
        return f"no binding for agent node '{node.name}'"
    expected_type = _BINDING_TYPE_BY_FANOUT_KIND[work.fanout.kind]
    if not isinstance(binding, expected_type):
        return (
            f"agent node '{node.name}' fans out {work.fanout.kind} but its binding is a "
            f"{type(binding).__name__}, not a {expected_type.__name__}"
        )
    return None


@pure
def find_binding_problems(pipeline: Pipeline, bindings: PipelineBindings) -> list[str]:
    """Describe every name of the pipeline the bindings leave unimplemented or implement with the wrong shape."""
    problems: list[str] = []
    for node in pipeline.nodes:
        match node.work:
            case AgentWork() as work:
                problem = _find_agent_binding_problem(node, work, bindings.agent_binding_by_node_name.get(node.name))
                if problem is not None:
                    problems.append(problem)
                for gate in work.gates:
                    if gate.name not in bindings.gate_binding_by_gate_name:
                        problems.append(f"no binding for gate '{gate.name}' of node '{node.name}'")
            case OrchestratorWork():
                if node.name not in bindings.orchestrator_binding_by_node_name:
                    problems.append(f"no binding for orchestrator node '{node.name}'")
            case _ as unreachable:
                assert_never(unreachable)
    return problems


def assert_pipeline_is_bound(pipeline: Pipeline, bindings: PipelineBindings) -> None:
    """Raise UnboundPipelineError before any agent is launched if the bindings do not cover the pipeline."""
    problems = find_binding_problems(pipeline, bindings)
    if problems:
        raise UnboundPipelineError(f"Pipeline '{pipeline.name}' cannot run: " + "; ".join(problems))

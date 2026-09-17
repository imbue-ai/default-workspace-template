"""The declarative pipeline model.

A ``Pipeline`` describes a whole agent pipeline as data: the parameters an
execution supplies, the artifacts the operator provides, the nodes and what
each of them waits for, fans out into, asks its agents, and must deliver.
Nodes carry no order; execution order is derived from the artifacts they need,
so the same object is both what an executor walks and what a diagram is drawn
from. Validators enforce the structural invariants, so an inconsistent
pipeline cannot be constructed and no agent is ever launched for one.
"""

import graphlib
from collections.abc import Iterable
from enum import auto
from typing import Literal
from typing import assert_never

from pydantic import Field
from pydantic import model_validator

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import UnitFloat
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr_mapreduce.primitives import ArtifactName
from imbue.mngr_mapreduce.primitives import NodeName
from imbue.mngr_mapreduce.prompts import PromptRenderError
from imbue.mngr_mapreduce.prompts import find_template_variables


class PipelineInvariantError(MngrError, ValueError):
    """Raised when a pipeline definition violates a structural invariant of the model."""

    ...


class NodeKind(UpperCaseStrEnum):
    """Who performs a node: a launched agent, or the orchestrator process itself.

    The value discriminates ``Node.work``, so it lives inside the variant that
    owns the fields it implies rather than beside them.
    """

    AGENT = auto()
    ORCHESTRATOR = auto()


class FanoutKind(UpperCaseStrEnum):
    """How a node decides how many jobs to run.

    The framework performs the fan-out arithmetic for each kind, so a binding
    supplies only the content of one job rather than re-deriving which jobs
    there should be.
    """

    SINGLE = auto()
    PER_UPSTREAM_JOB = auto()
    PER_PARTITION = auto()
    DISCOVERED = auto()


class UpstreamFanout(FrozenModel):
    """Shared by the fan-outs that run over the results of the node producing a named artifact.

    Naming the artifact is what makes the source unambiguous, so the node is free
    to need other things as well: a reviewer fans out over the mappers' branches
    and still reads the guide a setup node wrote.
    """

    over: ArtifactName = Field(
        description="The artifact whose producing node's results the fan-out runs over; must be one of the node's needs"
    )
    description: str = Field(description="How the fan-out reads to a person, e.g. 'one agent per feature file'")


class PerUpstreamJobFanout(UpstreamFanout):
    """One job per successful result of the named artifact's producer, minus those the binding declines."""

    kind: Literal[FanoutKind.PER_UPSTREAM_JOB] = FanoutKind.PER_UPSTREAM_JOB


class PerPartitionFanout(UpstreamFanout):
    """One job per distinct partition key over the named artifact's producer's results; the shuffle."""

    kind: Literal[FanoutKind.PER_PARTITION] = FanoutKind.PER_PARTITION


class SingleFanout(FrozenModel):
    """Exactly one job, whatever any upstream node produced."""

    kind: Literal[FanoutKind.SINGLE] = FanoutKind.SINGLE
    description: str = Field(description="How the fan-out reads to a person, e.g. 'the reducer, once'")


class DiscoveredFanout(FrozenModel):
    """As many jobs as the binding finds, over an input split the pipeline cannot see."""

    kind: Literal[FanoutKind.DISCOVERED] = FanoutKind.DISCOVERED
    description: str = Field(description="How the fan-out reads to a person, e.g. 'one agent per feature file'")


Fanout = PerUpstreamJobFanout | PerPartitionFanout | SingleFanout | DiscoveredFanout


class Artifact(FrozenModel):
    """A thing the operator supplies or the run produces: a branch, an outcome, evidence, a guide, the report."""

    name: ArtifactName = Field(description="Unique within the pipeline; nodes and outputs refer to the artifact by it")
    description: str = Field(description="What the artifact is and where it lives")


class Gate(FrozenModel):
    """A mechanical check run on each job a node produced; missing evidence fails it."""

    name: NonEmptyStr = Field(description="The gate's name in the pipeline's gate table")
    description: str = Field(description="What the gate checks")


class Parameter(FrozenModel):
    """One value an execution supplies, available to every node's prompt template."""

    name: NonEmptyStr = Field(description="The name the templates interpolate")
    description: str = Field(description="What the value is and where it comes from")
    default: str | None = Field(default=None, description="Used when the execution does not supply the parameter")


class AgentWork(FrozenModel):
    """What an agent node asks of its agents: how it fans out, what it tells them, and what counts as success."""

    kind: Literal[NodeKind.AGENT] = NodeKind.AGENT
    fanout: Fanout = Field(discriminator="kind", description="How the node decides how many jobs to run")
    prompt_template: NonEmptyStr = Field(description="Jinja source rendered into each job's prompt")
    job_variables: tuple[NonEmptyStr, ...] = Field(
        description="The template variable names the node's binding supplies per job, beyond the pipeline's parameters"
    )
    gates: tuple[Gate, ...] = Field(description="The mechanical checks run on each job the node produced")
    required_completion: UnitFloat = Field(
        default=UnitFloat(1.0),
        description="The fraction of the node's jobs that must succeed for the node to succeed",
    )
    min_job_count: NonNegativeInt = Field(
        default=NonNegativeInt(0),
        description="How many jobs the node must find at all; 0 means finding none is a legitimate no-op",
    )

    @model_validator(mode="after")
    def _validate_fanout_can_reach_min_job_count(self) -> "AgentWork":
        if self.fanout.kind is FanoutKind.SINGLE and self.min_job_count > 1:
            raise PipelineInvariantError(
                f"A node requires at least {self.min_job_count} jobs but fans out SINGLE, "
                f"which can never produce more than one"
            )
        return self


class OrchestratorWork(FrozenModel):
    """What a node does in the executor's own process.

    It launches no agent, so there is no prompt, no fan-out, no gate and no
    completion threshold to state: the absence of those fields is the point of
    the type.
    """

    kind: Literal[NodeKind.ORCHESTRATOR] = NodeKind.ORCHESTRATOR


class Node(FrozenModel):
    """One unit of work: what it waits for, what it must deliver, and what it does."""

    name: NodeName = Field(description="Unique within the pipeline; the node segment of its agent and branch names")
    summary: str = Field(description="What the node does, in a sentence or two")
    needs: tuple[ArtifactName, ...] = Field(
        description="The artifacts that must exist before the node runs; empty for a node that waits for nothing"
    )
    produces: tuple[Artifact, ...] = Field(description="The artifacts the node must deliver when it finishes")
    work: AgentWork | OrchestratorWork = Field(
        discriminator="kind", description="Who performs the node, and everything that depends on the answer"
    )

    @model_validator(mode="after")
    def _validate_gate_names_are_unique(self) -> "Node":
        if not isinstance(self.work, AgentWork):
            return self
        duplicate = _find_first_duplicate(gate.name for gate in self.work.gates)
        if duplicate is not None:
            raise PipelineInvariantError(f"Gate '{duplicate}' is listed more than once in node '{self.name}'")
        return self

    @model_validator(mode="after")
    def _validate_the_fanout_source_is_needed(self) -> "Node":
        """A node must declare the artifact it fans out over among the things it waits for."""
        match self.work:
            case AgentWork(fanout=UpstreamFanout() as fanout):
                if fanout.over not in self.needs:
                    raise PipelineInvariantError(
                        f"Node '{self.name}' fans out over '{fanout.over}', which is not among its needs"
                    )
            case AgentWork() | OrchestratorWork():
                pass
            case _ as unreachable:
                assert_never(unreachable)
        return self


class Pipeline(FrozenModel):
    """The whole pipeline as data: the parameters, the operator's inputs, the nodes, and what the run delivers."""

    name: str = Field(description="The pipeline's name")
    parameters: tuple[Parameter, ...] = Field(
        description="The context an execution supplies, available to every node's prompt template"
    )
    template_by_name: dict[str, str] = Field(
        default_factory=dict,
        description="Shared Jinja sources a node's prompt template may extend or include, keyed by the name it uses",
    )
    inputs: tuple[Artifact, ...] = Field(description="The artifacts the operator supplies before the run starts")
    nodes: tuple[Node, ...] = Field(
        description="The nodes, in no particular order; execution order is derived from what each one needs"
    )
    outputs: tuple[Artifact, ...] = Field(
        description="The artifacts the run delivers to the operator: node products by reference, plus anything the "
        "orchestrator writes over the whole run rather than in any node (like a report), defined only here"
    )

    @model_validator(mode="after")
    def _validate_names_are_unique(self) -> "Pipeline":
        for label, names in (
            ("Node", (node.name for node in self.nodes)),
            ("Parameter", (parameter.name for parameter in self.parameters)),
            ("Artifact", (artifact.name for artifact in _defined_artifacts(self))),
            ("Output", (output.name for output in self.outputs)),
        ):
            duplicate = _find_first_duplicate(names)
            if duplicate is not None:
                raise PipelineInvariantError(
                    f"{label} '{duplicate}' is listed more than once in pipeline '{self.name}'"
                )
        return self

    @model_validator(mode="after")
    def _validate_outputs_match_the_defined_artifacts_of_their_names(self) -> "Pipeline":
        defined_by_name = {artifact.name: artifact for artifact in _defined_artifacts(self)}
        for output in self.outputs:
            defined = defined_by_name.get(output.name)
            if defined is not None and defined != output:
                raise PipelineInvariantError(
                    f"Output '{output.name}' does not match the artifact of that name defined in pipeline "
                    f"'{self.name}'"
                )
        return self

    @model_validator(mode="after")
    def _validate_every_need_is_defined(self) -> "Pipeline":
        defined_names = {artifact.name for artifact in _defined_artifacts(self)}
        for node in self.nodes:
            for needed in node.needs:
                if needed not in defined_names:
                    raise PipelineInvariantError(
                        f"Node '{node.name}' needs '{needed}', which is neither a pipeline input nor produced "
                        f"by any node of pipeline '{self.name}'"
                    )
        return self

    @model_validator(mode="after")
    def _validate_the_graph_is_acyclic(self) -> "Pipeline":
        try:
            make_topological_sorter(self).prepare()
        except graphlib.CycleError as exc:
            cycle = " -> ".join(str(name) for name in exc.args[1])
            raise PipelineInvariantError(f"Pipeline '{self.name}' has a cycle: {cycle}") from exc
        return self

    @model_validator(mode="after")
    def _validate_the_fanout_source_is_produced_by_a_node(self) -> "Pipeline":
        """Fanning out over an operator-supplied input is meaningless: it has no results to fan over."""
        producer_by_artifact_name = producing_node_name_by_artifact_name(self)
        for node in self.nodes:
            if not isinstance(node.work, AgentWork) or not isinstance(node.work.fanout, UpstreamFanout):
                continue
            if node.work.fanout.over not in producer_by_artifact_name:
                raise PipelineInvariantError(
                    f"Node '{node.name}' fans out over '{node.work.fanout.over}', which no node of pipeline "
                    f"'{self.name}' produces, so there are no upstream results to fan out over"
                )
        return self

    @model_validator(mode="after")
    def _validate_every_template_variable_is_supplied(self) -> "Pipeline":
        parameter_names = {str(parameter.name) for parameter in self.parameters}
        for node in self.nodes:
            if not isinstance(node.work, AgentWork):
                continue
            available = parameter_names | {str(name) for name in node.work.job_variables}
            try:
                template_variables = find_template_variables(node.work.prompt_template, self.template_by_name)
            except PromptRenderError as exc:
                raise PipelineInvariantError(f"Node '{node.name}' has a bad prompt template: {exc}") from exc
            unsupplied = sorted(template_variables - available)
            if unsupplied:
                raise PipelineInvariantError(
                    f"The prompt template of node '{node.name}' reads {', '.join(unsupplied)}, which is neither a "
                    f"parameter of pipeline '{self.name}' nor one of the node's job variables"
                )
        return self


@pure
def producing_node_name_by_artifact_name(pipeline: Pipeline) -> dict[ArtifactName, NodeName]:
    """Which node produces each artifact; pipeline inputs are absent because the operator supplies them."""
    return {artifact.name: node.name for node in pipeline.nodes for artifact in node.produces}


@pure
def predecessor_names(pipeline: Pipeline, node: Node) -> frozenset[NodeName]:
    """The nodes that must finish before this one runs, derived from what it needs."""
    producer_by_artifact_name = producing_node_name_by_artifact_name(pipeline)
    return frozenset(producer_by_artifact_name[needed] for needed in node.needs if needed in producer_by_artifact_name)


def make_topological_sorter(pipeline: Pipeline) -> "graphlib.TopologicalSorter[NodeName]":
    """A sorter over the pipeline's derived graph, unprepared so the caller decides when cycles are reported."""
    sorter: graphlib.TopologicalSorter[NodeName] = graphlib.TopologicalSorter()
    for node in pipeline.nodes:
        sorter.add(node.name, *predecessor_names(pipeline, node))
    return sorter


@pure
def _find_first_duplicate(names: Iterable[str]) -> str | None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            return name
        seen.add(name)
    return None


@pure
def _defined_artifacts(pipeline: Pipeline) -> list[Artifact]:
    """Every artifact the pipeline's flow defines: the inputs and each node's products."""
    node_products = [artifact for node in pipeline.nodes for artifact in node.produces]
    return [*pipeline.inputs, *node_products]

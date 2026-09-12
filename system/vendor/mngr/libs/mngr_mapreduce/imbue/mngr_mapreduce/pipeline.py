"""The declarative multi-stage pipeline model.

A ``Pipeline`` describes a whole agent pipeline as data: the artifacts the
operator supplies, the stages in execution order (each with its steps, the
artifacts they produce, and the mechanical gates run on its branches), and
the artifacts the run delivers. Validators enforce the structural invariants
so an inconsistent pipeline cannot be constructed. A stage executor iterates
the same object that diagrams (``pipeline_svg.py``) are rendered from.
"""

from collections.abc import Iterable
from enum import auto

from pydantic import Field
from pydantic import model_validator

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr_mapreduce.primitives import ArtifactName


class PipelineInvariantError(MngrError, ValueError):
    """Raised when a pipeline definition violates a structural invariant of the stage model."""

    ...


class Actor(UpperCaseStrEnum):
    """Who performs a step: a launched agent, or the orchestrator process itself."""

    AGENT = auto()
    ORCHESTRATOR = auto()


class Artifact(FrozenModel):
    """A thing the operator supplies or the run produces: a branch, an outcome, evidence, a guide, the report."""

    name: ArtifactName = Field(description="Unique within the pipeline; steps and outputs refer to the artifact by it")
    description: str = Field(description="What the artifact is and where it lives")


class Gate(FrozenModel):
    """A mechanical check run after a stage on each branch it produced; missing evidence fails it."""

    name: NonEmptyStr = Field(description="The gate's name in the pipeline's gate table")
    description: str = Field(description="What the gate checks")


class Step(FrozenModel):
    """One unit of work inside a stage, performed by an agent or by the orchestrator."""

    name: NonEmptyStr = Field(description="What the step does, in one word")
    actor: Actor = Field(description="Who performs the step")
    base: ArtifactName = Field(
        description="The artifact the step starts from: a pipeline input, or a branch produced before the step runs"
    )
    produces: tuple[Artifact, ...] = Field(description="The artifacts the step must deliver when it finishes")


class Stage(FrozenModel):
    """One pipeline stage: how it fans out, its steps, and the gates run on its branches before the next stage."""

    name: NonEmptyStr = Field(description="The stage's name; also the stage segment of its branch and agent names")
    fanout: NonEmptyStr = Field(description="How many agents the stage launches, e.g. 'one agent per feature file'")
    summary: str = Field(description="What the stage does, in a sentence or two")
    steps: tuple[Step, ...] = Field(description="The stage's units of work, in execution order")
    gates: tuple[Gate, ...] = Field(description="The mechanical checks run on each branch the stage produced")

    @model_validator(mode="after")
    def _validate_gate_names_are_unique(self) -> "Stage":
        duplicate = _find_first_duplicate(gate.name for gate in self.gates)
        if duplicate is not None:
            raise PipelineInvariantError(f"Gate '{duplicate}' is listed more than once in stage '{self.name}'")
        return self


class Pipeline(FrozenModel):
    """The whole pipeline as data: what the operator supplies, the stages in order, and what the run delivers."""

    name: str = Field(description="The pipeline's name")
    inputs: tuple[Artifact, ...] = Field(description="The artifacts the operator supplies before the run starts")
    stages: tuple[Stage, ...] = Field(description="The stages in execution order")
    outputs: tuple[Artifact, ...] = Field(
        description="The artifacts the run delivers to the operator: step products by reference, plus anything the "
        "orchestrator writes over the whole run rather than in any stage (like a report), defined only here"
    )

    @model_validator(mode="after")
    def _validate_stage_names_are_unique(self) -> "Pipeline":
        duplicate = _find_first_duplicate(stage.name for stage in self.stages)
        if duplicate is not None:
            raise PipelineInvariantError(f"Stage '{duplicate}' is listed more than once in pipeline '{self.name}'")
        return self

    @model_validator(mode="after")
    def _validate_artifact_names_are_unique(self) -> "Pipeline":
        duplicate = _find_first_duplicate(artifact.name for artifact in _defined_artifacts(self))
        if duplicate is not None:
            raise PipelineInvariantError(f"Artifact '{duplicate}' is defined more than once in pipeline '{self.name}'")
        return self

    @model_validator(mode="after")
    def _validate_step_bases_exist_before_the_step_runs(self) -> "Pipeline":
        available_names = {artifact.name for artifact in self.inputs}
        for stage in self.stages:
            for step in stage.steps:
                if step.base not in available_names:
                    raise PipelineInvariantError(
                        f"Step '{stage.name}/{step.name}' starts from '{step.base}', which is neither a pipeline "
                        f"input nor an artifact produced before the step runs"
                    )
                available_names.update(artifact.name for artifact in step.produces)
        return self

    @model_validator(mode="after")
    def _validate_output_names_are_unique(self) -> "Pipeline":
        duplicate = _find_first_duplicate(output.name for output in self.outputs)
        if duplicate is not None:
            raise PipelineInvariantError(f"Output '{duplicate}' is listed more than once in pipeline '{self.name}'")
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
    """Every artifact the pipeline's flow defines: the inputs and each step's products."""
    step_products = [artifact for stage in pipeline.stages for step in stage.steps for artifact in step.produces]
    return [*pipeline.inputs, *step_products]

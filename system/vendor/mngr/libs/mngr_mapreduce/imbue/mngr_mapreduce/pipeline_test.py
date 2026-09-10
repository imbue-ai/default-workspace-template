import pytest
from pydantic import ValidationError

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.mngr_mapreduce.pipeline import Actor
from imbue.mngr_mapreduce.pipeline import Artifact
from imbue.mngr_mapreduce.pipeline import Gate
from imbue.mngr_mapreduce.pipeline import Pipeline
from imbue.mngr_mapreduce.pipeline import Stage
from imbue.mngr_mapreduce.pipeline import Step
from imbue.mngr_mapreduce.primitives import ArtifactName


def _artifact(name: str) -> Artifact:
    return Artifact(name=ArtifactName(name), description=f"the {name}")


def _gate(name: str) -> Gate:
    return Gate(name=NonEmptyStr(name), description=f"checks {name}")


def _agent_step(name: str, base: str, produces: tuple[Artifact, ...]) -> Step:
    return Step(name=NonEmptyStr(name), actor=Actor.AGENT, base=ArtifactName(base), produces=produces)


def _stage(name: str, steps: tuple[Step, ...], gates: tuple[Gate, ...]) -> Stage:
    return Stage(
        name=NonEmptyStr(name),
        fanout=NonEmptyStr("one agent"),
        summary=f"the {name} stage",
        steps=steps,
        gates=gates,
    )


def _pipeline(inputs: tuple[Artifact, ...], stages: tuple[Stage, ...], outputs: tuple[Artifact, ...]) -> Pipeline:
    return Pipeline(name="p", inputs=inputs, stages=stages, outputs=outputs)


def test_pipeline_rejects_two_stages_with_the_same_name() -> None:
    seed = _artifact("seed")
    first_build = _stage("build", (_agent_step("go", "seed", (_artifact("built"),)),), ())
    second_build = _stage("build", (_agent_step("go", "seed", (_artifact("built_again"),)),), ())

    with pytest.raises(ValidationError, match="Stage 'build' is listed more than once in pipeline 'p'"):
        _pipeline((seed,), (first_build, second_build), ())


def test_pipeline_rejects_an_artifact_that_is_both_an_input_and_a_step_product() -> None:
    seed = _artifact("seed")
    build = _stage("build", (_agent_step("go", "seed", (_artifact("seed"),)),), ())

    with pytest.raises(ValidationError, match="Artifact 'seed' is defined more than once in pipeline 'p'"):
        _pipeline((seed,), (build,), ())


def test_pipeline_rejects_an_artifact_produced_by_two_steps() -> None:
    seed = _artifact("seed")
    first = _stage("first", (_agent_step("go", "seed", (_artifact("built"),)),), ())
    second = _stage("second", (_agent_step("go", "seed", (_artifact("built"),)),), ())

    with pytest.raises(ValidationError, match="Artifact 'built' is defined more than once in pipeline 'p'"):
        _pipeline((seed,), (first, second), ())


def test_pipeline_rejects_a_step_whose_base_nothing_defines() -> None:
    build = _stage("build", (_agent_step("go", "ghost", (_artifact("built"),)),), ())

    with pytest.raises(
        ValidationError,
        match="Step 'build/go' starts from 'ghost', which is neither a pipeline input nor an artifact produced before",
    ):
        _pipeline((_artifact("seed"),), (build,), ())


def test_pipeline_rejects_a_step_whose_base_is_produced_only_by_a_later_stage() -> None:
    first = _stage("first", (_agent_step("go", "later", (_artifact("early"),)),), ())
    second = _stage("second", (_agent_step("go", "early", (_artifact("later"),)),), ())

    with pytest.raises(ValidationError, match="Step 'first/go' starts from 'later'"):
        _pipeline((_artifact("seed"),), (first, second), ())


def test_pipeline_accepts_a_step_that_starts_from_an_earlier_step_of_the_same_stage() -> None:
    seed = _artifact("seed")
    prepared = _artifact("prepared")
    finished = _artifact("finished")
    build = _stage(
        "build",
        (_agent_step("prepare", "seed", (prepared,)), _agent_step("finish", "prepared", (finished,))),
        (),
    )

    pipeline = _pipeline((seed,), (build,), (finished,))

    assert pipeline.stages[0].steps[1].base == "prepared"


def test_stage_rejects_the_same_gate_listed_twice() -> None:
    with pytest.raises(ValidationError, match="Gate 'lint' is listed more than once in stage 'build'"):
        _stage("build", (), (_gate("lint"), _gate("paths"), _gate("lint")))


def test_pipeline_rejects_the_same_output_listed_twice() -> None:
    seed = _artifact("seed")
    built = _artifact("built")
    build = _stage("build", (_agent_step("go", "seed", (built,)),), ())

    with pytest.raises(ValidationError, match="Output 'built' is listed more than once in pipeline 'p'"):
        _pipeline((seed,), (build,), (built, built))


def test_pipeline_rejects_an_output_whose_description_differs_from_the_defined_artifact() -> None:
    seed = _artifact("seed")
    build = _stage("build", (_agent_step("go", "seed", (_artifact("built"),)),), ())
    lookalike = Artifact(name=ArtifactName("built"), description="something else entirely")

    with pytest.raises(
        ValidationError, match="Output 'built' does not match the artifact of that name defined in pipeline 'p'"
    ):
        _pipeline((seed,), (build,), (lookalike,))


def test_pipeline_accepts_an_output_that_no_step_produces_as_a_run_level_artifact() -> None:
    report = _artifact("report")

    pipeline = Pipeline(name="p", inputs=(_artifact("seed"),), stages=(), outputs=(report,))

    assert Pipeline.model_validate(pipeline.model_dump()).outputs == (report,)

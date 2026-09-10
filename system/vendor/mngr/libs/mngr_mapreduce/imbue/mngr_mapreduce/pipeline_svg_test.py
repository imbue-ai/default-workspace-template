import xml.etree.ElementTree as ElementTree

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.mngr_mapreduce.pipeline import Actor
from imbue.mngr_mapreduce.pipeline import Artifact
from imbue.mngr_mapreduce.pipeline import Gate
from imbue.mngr_mapreduce.pipeline import Pipeline
from imbue.mngr_mapreduce.pipeline import Stage
from imbue.mngr_mapreduce.pipeline import Step
from imbue.mngr_mapreduce.pipeline_svg import render_pipeline_svg
from imbue.mngr_mapreduce.primitives import ArtifactName

_SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"
_SEED = Artifact(name=ArtifactName("seed"), description="the seed")

_SOURCE = Artifact(name=ArtifactName("source"), description="the operator checkout the run starts from")
_PLAN_BRANCH = Artifact(name=ArtifactName("plan_branch"), description="the branch the planner publishes")
_PLAN_NOTES = Artifact(name=ArtifactName("plan_notes"), description="what the planner decided and why")
_MERGED = Artifact(name=ArtifactName("merged"), description="every plan branch merged together")
_FINAL = Artifact(name=ArtifactName("final"), description="the polished deliverable branch")
_LOG = Artifact(name=ArtifactName("log"), description="the run log the orchestrator appends to throughout")

_LINT_GATE = Gate(name=NonEmptyStr("lint"), description="lint passes on the touched files")
_EVIDENCE_GATE = Gate(name=NonEmptyStr("evidence"), description="the archive contains the required evidence")

_PLAN_STAGE = Stage(
    name=NonEmptyStr("plan"),
    fanout=NonEmptyStr("one agent per plan"),
    summary="One agent per feature file plans the work and publishes a branch with its notes.",
    steps=(
        Step(
            name=NonEmptyStr("plan"),
            actor=Actor.AGENT,
            base=ArtifactName("source"),
            produces=(_PLAN_BRANCH, _PLAN_NOTES),
        ),
    ),
    gates=(_LINT_GATE, _EVIDENCE_GATE),
)
_FINISH_STAGE = Stage(
    name=NonEmptyStr("finish"),
    fanout=NonEmptyStr("one agent"),
    summary="The orchestrator merges every plan branch, then one agent polishes the result.",
    steps=(
        Step(
            name=NonEmptyStr("integrate"),
            actor=Actor.ORCHESTRATOR,
            base=ArtifactName("plan_branch"),
            produces=(_MERGED,),
        ),
        Step(name=NonEmptyStr("polish"), actor=Actor.AGENT, base=ArtifactName("merged"), produces=(_FINAL,)),
    ),
    gates=(_LINT_GATE,),
)

# A small pipeline exercising every rendered element: an agent and an orchestrator
# step, a multi-step stage, gated and ungated connectors, and a run-level output.
_SAMPLE_PIPELINE = Pipeline(
    name="sample",
    inputs=(_SOURCE,),
    stages=(_PLAN_STAGE, _FINISH_STAGE),
    outputs=(_FINAL, _LOG),
)


def _parse(svg: str) -> ElementTree.Element:
    return ElementTree.fromstring(svg)


def _visible_lines(root: ElementTree.Element) -> list[str]:
    """What a viewer sees, one entry per text element with its runs space-separated; tooltips excluded."""
    return [" ".join(run for run in element.itertext() if run) for element in root.iter(f"{_SVG_NAMESPACE}text")]


def _visible_text(root: ElementTree.Element) -> str:
    return "\n".join(_visible_lines(root))


def _single_stage_pipeline(step: Step, gates: tuple[Gate, ...]) -> Pipeline:
    stage = Stage(
        name=NonEmptyStr("build"), fanout=NonEmptyStr("one agent"), summary="builds", steps=(step,), gates=gates
    )
    return Pipeline(name="p", inputs=(_SEED,), stages=(stage,), outputs=step.produces)


def test_render_pipeline_svg_is_well_formed_svg_at_the_designed_width() -> None:
    root = _parse(render_pipeline_svg(_SAMPLE_PIPELINE))

    assert root.tag == f"{_SVG_NAMESPACE}svg"
    assert root.get("width") == "960"
    assert root.get("viewBox") == f"0 0 960 {root.get('height')}"


def test_render_pipeline_svg_shows_every_stage_step_artifact_gate_and_fanout() -> None:
    visible = _visible_text(_parse(render_pipeline_svg(_SAMPLE_PIPELINE)))

    for stage in _SAMPLE_PIPELINE.stages:
        assert stage.name in visible
        assert stage.fanout in visible
        for gate in stage.gates:
            assert gate.name in visible
        for step in stage.steps:
            assert step.name in visible
            assert step.base in visible
            for artifact in step.produces:
                assert artifact.name in visible
    for artifact in (*_SAMPLE_PIPELINE.inputs, *_SAMPLE_PIPELINE.outputs):
        assert artifact.name in visible


def test_render_pipeline_svg_lays_out_inputs_then_each_stage_then_outputs_top_to_bottom() -> None:
    root = _parse(render_pipeline_svg(_SAMPLE_PIPELINE))

    titles = [
        "".join(element.itertext())
        for element in root.iter(f"{_SVG_NAMESPACE}text")
        if element.get("font-size") == "15"
    ]

    assert titles == [
        "inputs from the operator",
        "plan",
        "finish",
        "outputs delivered to the operator",
    ]


def test_render_pipeline_svg_names_each_step_with_its_actor_and_base() -> None:
    lines = _visible_lines(_parse(render_pipeline_svg(_SAMPLE_PIPELINE)))

    assert "integrate orchestrator" in lines
    assert "from plan_branch" in lines
    assert "polish agent" in lines
    assert "from merged" in lines


def test_render_pipeline_svg_carries_artifact_and_gate_descriptions_as_tooltips() -> None:
    root = _parse(render_pipeline_svg(_SAMPLE_PIPELINE))

    tooltips = "\n".join("".join(title.itertext()) for title in root.iter(f"{_SVG_NAMESPACE}title"))

    for stage in _SAMPLE_PIPELINE.stages:
        for step in stage.steps:
            for artifact in step.produces:
                assert f"{artifact.name}: {artifact.description}" in tooltips
        for gate in stage.gates:
            assert gate.description in tooltips


def test_render_pipeline_svg_is_deterministic() -> None:
    same_model_again = Pipeline.model_validate(_SAMPLE_PIPELINE.model_dump())

    assert render_pipeline_svg(_SAMPLE_PIPELINE) == render_pipeline_svg(same_model_again)


def test_render_pipeline_svg_escapes_markup_in_the_model_text() -> None:
    hostile = 'a <b>bold</b> & "quoted" claim'
    pipeline = Pipeline(
        name="p",
        inputs=(Artifact(name=ArtifactName("seed"), description=hostile),),
        stages=(),
        outputs=(),
    )

    svg = render_pipeline_svg(pipeline)

    assert "<b>" not in svg
    assert hostile in _visible_text(_parse(svg))


def test_render_pipeline_svg_starts_the_description_below_an_overlong_input_name() -> None:
    overlong_name = "an_input_whose_name_alone_fills_the_row_" + "x" * 40
    description = "so its description begins on the next line"
    pipeline = Pipeline(
        name="p",
        inputs=(Artifact(name=ArtifactName(overlong_name), description=description),),
        stages=(),
        outputs=(),
    )

    lines = _visible_lines(_parse(render_pipeline_svg(pipeline)))

    assert overlong_name in lines
    assert description in lines


def test_render_pipeline_svg_wraps_a_long_product_list_onto_further_lines() -> None:
    produced = tuple(
        Artifact(name=ArtifactName(f"product_number_{idx}_of_the_step"), description="made") for idx in range(8)
    )
    step = Step(name=NonEmptyStr("go"), actor=Actor.AGENT, base=ArtifactName("seed"), produces=produced)

    lines = _visible_lines(_parse(render_pipeline_svg(_single_stage_pipeline(step, ()))))

    lines_naming_products = [line for line in lines if "product_number_" in line]
    assert len(lines_naming_products) >= 2
    assert all(len(line) <= 80 for line in lines_naming_products)


def test_render_pipeline_svg_wraps_gate_pills_onto_further_rows_inside_the_card_width() -> None:
    gates = tuple(
        Gate(name=NonEmptyStr(f"gate_number_{idx}_with_a_long_name"), description="checks") for idx in range(9)
    )
    step = Step(name=NonEmptyStr("go"), actor=Actor.AGENT, base=ArtifactName("seed"), produces=())

    root = _parse(render_pipeline_svg(_single_stage_pipeline(step, gates)))

    pills = [rect for rect in root.iter(f"{_SVG_NAMESPACE}rect") if rect.get("fill") == "#f0f9f2"]
    assert len(pills) == len(gates)
    assert len({pill.get("y") for pill in pills}) >= 2
    assert max(int(pill.get("x", "0")) + int(pill.get("width", "0")) for pill in pills) <= 920

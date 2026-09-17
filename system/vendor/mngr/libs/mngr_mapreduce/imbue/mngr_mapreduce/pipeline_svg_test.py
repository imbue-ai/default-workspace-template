import xml.etree.ElementTree as ElementTree

from imbue.mngr_mapreduce.pipeline import FanoutKind
from imbue.mngr_mapreduce.pipeline import Pipeline
from imbue.mngr_mapreduce.pipeline_svg import rank_by_node_name
from imbue.mngr_mapreduce.pipeline_svg import render_pipeline_svg
from imbue.mngr_mapreduce.primitives import NodeName
from imbue.mngr_mapreduce.testing import make_artifact
from imbue.mngr_mapreduce.testing import make_node
from imbue.mngr_mapreduce.testing import make_pipeline

_SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"


def _diamond() -> Pipeline:
    return make_pipeline(
        (
            make_node("source", needs=("seed",), produces=("a",)),
            make_node("left", needs=("a",), produces=("b",), kind=FanoutKind.PER_UPSTREAM_JOB),
            make_node("right", needs=("a",), produces=("c",), kind=FanoutKind.PER_UPSTREAM_JOB),
            make_node("sink", needs=("b", "c"), produces=("d",)),
        ),
        outputs=(make_artifact("d"),),
    )


def _parse(svg: str) -> ElementTree.Element:
    return ElementTree.fromstring(svg)


def _visible_text(root: ElementTree.Element) -> list[str]:
    return [element.text or "" for element in root.iter(f"{_SVG_NAMESPACE}text")]


def test_rank_puts_a_diamonds_two_middle_nodes_in_one_rank() -> None:
    """Nodes that can run together share a row, which is what a reader checks the topology by."""
    ranks = rank_by_node_name(_diamond())

    assert ranks[NodeName("source")] == 0
    assert ranks[NodeName("left")] == ranks[NodeName("right")] == 1
    assert ranks[NodeName("sink")] == 2


def test_rank_uses_the_longest_path_so_every_edge_points_downward() -> None:
    """A node must sit below everything it waits for, not merely below the nearest one."""
    pipeline = make_pipeline(
        (
            make_node("first", needs=("seed",), produces=("a",)),
            make_node("second", needs=("a",), produces=("b",), kind=FanoutKind.PER_UPSTREAM_JOB),
            make_node("last", needs=("a", "b"), produces=("c",)),
        )
    )

    ranks = rank_by_node_name(pipeline)

    assert ranks[NodeName("last")] == 2


def test_render_produces_well_formed_svg_with_a_box_per_node() -> None:
    root = _parse(render_pipeline_svg(_diamond()))

    assert root.tag == f"{_SVG_NAMESPACE}svg"
    # One rect per node, plus the background and the two terminals.
    assert len(list(root.iter(f"{_SVG_NAMESPACE}rect"))) == 4 + 3


def test_render_names_every_node_and_labels_every_edge_with_its_artifact() -> None:
    text = _visible_text(_parse(render_pipeline_svg(_diamond())))

    for node_name in ("source", "left", "right", "sink"):
        assert node_name in text
    for artifact_name in ("seed", "a", "b", "c", "d"):
        assert artifact_name in text


def test_render_shows_each_nodes_fanout_and_thresholds() -> None:
    pipeline = make_pipeline(
        (make_node("map", needs=("seed",), produces=("a",), required_completion=0.9, min_job_count=1),)
    )

    text = _visible_text(_parse(render_pipeline_svg(pipeline)))

    assert "DISCOVERED" in text
    assert "needs 90% to succeed, min 1" in text


def test_render_is_deterministic() -> None:
    """Rendering the same pipeline twice gives the same bytes, so a diagram can be diffed against its model."""
    assert render_pipeline_svg(_diamond()) == render_pipeline_svg(_diamond())


def test_render_distinguishes_an_orchestrator_node_from_an_agent_node() -> None:
    pipeline = make_pipeline(
        (
            make_node("map", needs=("seed",), produces=("a",)),
            make_node("integrate", needs=("a",), produces=("b",), is_orchestrator=True),
        )
    )

    svg = render_pipeline_svg(pipeline)

    assert "#6b46c1" in svg
    assert "#3b6fd4" in svg

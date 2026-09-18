"""A deterministic, dependency-free SVG renderer for the pipeline model.

Draws a ``Pipeline`` as a layered graph: the operator's inputs on the top row,
then the nodes rank by rank, then the run's outputs. A node's rank is the
longest path to it from any source, so every edge points strictly downward and
nodes that can run together share a row. Within a rank, nodes keep their order
in ``Pipeline.nodes``, which is what makes the output a pure function of the
model.
"""

from collections.abc import Sequence
from textwrap import shorten
from typing import Final
from xml.sax.saxutils import escape

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_mapreduce.pipeline import AgentWork
from imbue.mngr_mapreduce.pipeline import Node
from imbue.mngr_mapreduce.pipeline import Pipeline
from imbue.mngr_mapreduce.pipeline import predecessor_names
from imbue.mngr_mapreduce.pipeline import producing_node_name_by_artifact_name
from imbue.mngr_mapreduce.primitives import ArtifactName
from imbue.mngr_mapreduce.primitives import NodeName

# Geometry, in px.
_MARGIN: Final[int] = 28
_NODE_WIDTH: Final[int] = 240
_NODE_HEIGHT: Final[int] = 96
_NODE_GAP_X: Final[int] = 36
_RANK_GAP_Y: Final[int] = 84
_TERMINAL_HEIGHT: Final[int] = 34
_TERMINAL_GAP_Y: Final[int] = 56
_CORNER_RADIUS: Final[int] = 7
_LINE_HEIGHT: Final[int] = 16
_TEXT_PADDING: Final[int] = 12
_MAX_LABEL_CHARS: Final[int] = 34

_TITLE_FONT_SIZE: Final[float] = 14
_BODY_FONT_SIZE: Final[float] = 11.5
_EDGE_FONT_SIZE: Final[float] = 10.5
_FONT_FAMILY: Final[str] = "ui-sans-serif, system-ui, -apple-system, Segoe UI, Helvetica, Arial, sans-serif"

_AGENT_FILL: Final[str] = "#eef4ff"
_AGENT_STROKE: Final[str] = "#3b6fd4"
_ORCHESTRATOR_FILL: Final[str] = "#f3eeff"
_ORCHESTRATOR_STROKE: Final[str] = "#6b46c1"
_TERMINAL_FILL: Final[str] = "#f5f5f5"
_TERMINAL_STROKE: Final[str] = "#9aa0a6"
_EDGE_STROKE: Final[str] = "#7a8088"
_TEXT_COLOR: Final[str] = "#1f2328"
_MUTED_COLOR: Final[str] = "#57606a"


class NodeBox(FrozenModel):
    """One node's placed rectangle."""

    node: Node = Field(description="The node the box draws")
    rank: int = Field(description="Which row the box sits in; the longest path to the node from any source")
    x: int = Field(description="Left edge, in px")
    y: int = Field(description="Top edge, in px")

    @property
    def center_x(self) -> int:
        return self.x + _NODE_WIDTH // 2

    @property
    def bottom_y(self) -> int:
        return self.y + _NODE_HEIGHT


@pure
def rank_by_node_name(pipeline: Pipeline) -> dict[NodeName, int]:
    """The longest path to each node from any source, so every edge points strictly downward.

    Longest path rather than shortest: a node must sit below *every* node it
    waits for, not merely below the nearest one.
    """
    ranks: dict[NodeName, int] = {}
    for node in _nodes_in_dependency_order(pipeline):
        predecessors = predecessor_names(pipeline, node)
        ranks[node.name] = 1 + max((ranks[name] for name in predecessors), default=-1)
    return ranks


@pure
def _nodes_in_dependency_order(pipeline: Pipeline) -> list[Node]:
    """The pipeline's nodes, each preceded by everything it waits for."""
    node_by_name = {node.name: node for node in pipeline.nodes}
    ordered: list[Node] = []
    remaining = list(pipeline.nodes)
    placed: set[NodeName] = set()
    while remaining:
        ready = [node for node in remaining if predecessor_names(pipeline, node) <= placed]
        for node in ready:
            ordered.append(node_by_name[node.name])
            placed.add(node.name)
        remaining = [node for node in remaining if node.name not in placed]
    return ordered


@pure
def _place_nodes(pipeline: Pipeline, width: int) -> list[NodeBox]:
    """One box per node, laid out rank by rank and centred within each row."""
    ranks = rank_by_node_name(pipeline)
    nodes_by_rank: dict[int, list[Node]] = {}
    for node in pipeline.nodes:
        nodes_by_rank.setdefault(ranks[node.name], []).append(node)
    boxes: list[NodeBox] = []
    top = _MARGIN + _TERMINAL_HEIGHT + _TERMINAL_GAP_Y
    for rank in sorted(nodes_by_rank):
        row = nodes_by_rank[rank]
        row_width = len(row) * _NODE_WIDTH + (len(row) - 1) * _NODE_GAP_X
        left = (width - row_width) // 2
        for column_idx, node in enumerate(row):
            boxes.append(
                NodeBox(
                    node=node,
                    rank=rank,
                    x=left + column_idx * (_NODE_WIDTH + _NODE_GAP_X),
                    y=top + rank * (_NODE_HEIGHT + _RANK_GAP_Y),
                )
            )
    return boxes


@pure
def _diagram_width(pipeline: Pipeline) -> int:
    ranks = rank_by_node_name(pipeline)
    widest_row = max(
        (sum(1 for node in pipeline.nodes if ranks[node.name] == rank) for rank in ranks.values()), default=1
    )
    return max(640, 2 * _MARGIN + widest_row * _NODE_WIDTH + (widest_row - 1) * _NODE_GAP_X)


def render_pipeline_svg(pipeline: Pipeline) -> str:
    """Render the pipeline to an SVG document, deterministically."""
    width = _diagram_width(pipeline)
    boxes = _place_nodes(pipeline, width)
    bottom_of_nodes = max((box.bottom_y for box in boxes), default=_MARGIN + _TERMINAL_HEIGHT)
    outputs_y = bottom_of_nodes + _TERMINAL_GAP_Y
    height = outputs_y + _TERMINAL_HEIGHT + _MARGIN
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="{_FONT_FAMILY}">',
        _render_defs(),
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        _render_terminal(width, _MARGIN, "inputs", [artifact.name for artifact in pipeline.inputs]),
        *_render_edges(pipeline, boxes, width, outputs_y),
        *(_render_node_box(box) for box in boxes),
        _render_terminal(width, outputs_y, "outputs", [artifact.name for artifact in pipeline.outputs]),
        "</svg>",
    ]
    return "\n".join(parts) + "\n"


@pure
def _render_defs() -> str:
    return (
        "<defs>"
        '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        'orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{_EDGE_STROKE}"/>'
        "</marker>"
        "</defs>"
    )


@pure
def _render_terminal(width: int, y: int, label: str, names: Sequence[ArtifactName]) -> str:
    text = f"{label}: {', '.join(str(name) for name in names)}" if names else f"{label}: none"
    return (
        f'<g><rect x="{_MARGIN}" y="{y}" width="{width - 2 * _MARGIN}" height="{_TERMINAL_HEIGHT}" '
        f'rx="{_TERMINAL_HEIGHT // 2}" fill="{_TERMINAL_FILL}" stroke="{_TERMINAL_STROKE}"/>'
        f'<text x="{width // 2}" y="{y + _TERMINAL_HEIGHT // 2 + 4}" text-anchor="middle" '
        f'font-size="{_BODY_FONT_SIZE}" fill="{_MUTED_COLOR}">{escape(text)}</text></g>'
    )


@pure
def _render_node_box(box: NodeBox) -> str:
    work = box.node.work
    is_agent = isinstance(work, AgentWork)
    fill = _AGENT_FILL if is_agent else _ORCHESTRATOR_FILL
    stroke = _AGENT_STROKE if is_agent else _ORCHESTRATOR_STROKE
    lines = (
        [
            (str(work.fanout.kind), _BODY_FONT_SIZE, _TEXT_COLOR),
            (_shorten(work.fanout.description), _BODY_FONT_SIZE, _MUTED_COLOR),
            (_shorten(_describe_thresholds(work)), _BODY_FONT_SIZE, _MUTED_COLOR),
            (_shorten(_describe_gates(work)), _BODY_FONT_SIZE, _MUTED_COLOR),
        ]
        if isinstance(work, AgentWork)
        else [("ORCHESTRATOR", _BODY_FONT_SIZE, _TEXT_COLOR), ("runs on the executor", _BODY_FONT_SIZE, _MUTED_COLOR)]
    )
    text_x = box.x + _TEXT_PADDING
    body = "".join(
        f'<text x="{text_x}" y="{box.y + 44 + idx * _LINE_HEIGHT}" font-size="{size}" fill="{color}">'
        f"{escape(text)}</text>"
        for idx, (text, size, color) in enumerate(lines)
    )
    return (
        f'<g><rect x="{box.x}" y="{box.y}" width="{_NODE_WIDTH}" height="{_NODE_HEIGHT}" rx="{_CORNER_RADIUS}" '
        f'fill="{fill}" stroke="{stroke}"/>'
        f'<text x="{text_x}" y="{box.y + 24}" font-size="{_TITLE_FONT_SIZE}" font-weight="600" '
        f'fill="{_TEXT_COLOR}">{escape(str(box.node.name))}</text>'
        f"{body}</g>"
    )


@pure
def _describe_thresholds(work: AgentWork) -> str:
    completion = f"needs {work.required_completion:.0%} to succeed"
    return completion if work.min_job_count == 0 else f"{completion}, min {work.min_job_count}"


@pure
def _describe_gates(work: AgentWork) -> str:
    return "gates: " + (", ".join(str(gate.name) for gate in work.gates) if work.gates else "none")


@pure
def _shorten(text: str) -> str:
    return shorten(text, width=_MAX_LABEL_CHARS, placeholder="...")


@pure
def _render_edges(pipeline: Pipeline, boxes: Sequence[NodeBox], width: int, outputs_y: int) -> list[str]:
    """One arrow per dependency, labelled with the artifact that carries it, plus the terminals' arrows."""
    box_by_name = {box.node.name: box for box in boxes}
    producer_by_artifact = producing_node_name_by_artifact_name(pipeline)
    edges: list[str] = []
    for box in boxes:
        for needed in box.node.needs:
            producer = producer_by_artifact.get(needed)
            if producer is not None:
                source = box_by_name[producer]
                edges.append(_render_edge(source.center_x, source.bottom_y, box.center_x, box.y, str(needed)))
            else:
                # Validation guarantees a need with no producer is a pipeline input.
                edges.append(_render_edge(width // 2, _MARGIN + _TERMINAL_HEIGHT, box.center_x, box.y, str(needed)))
    produced_output_names = [output.name for output in pipeline.outputs if output.name in producer_by_artifact]
    for output_name in produced_output_names:
        source = box_by_name[producer_by_artifact[output_name]]
        edges.append(_render_edge(source.center_x, source.bottom_y, width // 2, outputs_y, str(output_name)))
    return edges


@pure
def _render_edge(x1: int, y1: int, x2: int, y2: int, label: str) -> str:
    midpoint_x = (x1 + x2) // 2
    midpoint_y = (y1 + y2) // 2
    return (
        f'<g><path d="M {x1} {y1} C {x1} {midpoint_y}, {x2} {midpoint_y}, {x2} {y2}" fill="none" '
        f'stroke="{_EDGE_STROKE}" stroke-width="1.4" marker-end="url(#arrow)"/>'
        f'<text x="{midpoint_x + 6}" y="{midpoint_y}" font-size="{_EDGE_FONT_SIZE}" fill="{_MUTED_COLOR}">'
        f"{escape(label)}</text></g>"
    )

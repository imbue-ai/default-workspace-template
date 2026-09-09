"""A deterministic, dependency-free SVG renderer for the declarative pipeline model.

Draws a ``Pipeline`` as a vertical flow: its inputs, one card per stage joined
by arrows carrying the stage's gates, and its outputs. The output is a pure
function of the model, so a committed diagram can be pinned to its pipeline by
a drift test and regenerated with ``scripts/render_pipeline_svg.py``.
"""

from collections.abc import Sequence
from itertools import accumulate
from textwrap import wrap
from typing import Final
from typing import assert_never
from xml.sax.saxutils import escape

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_mapreduce.pipeline import Actor
from imbue.mngr_mapreduce.pipeline import Artifact
from imbue.mngr_mapreduce.pipeline import Gate
from imbue.mngr_mapreduce.pipeline import Pipeline
from imbue.mngr_mapreduce.pipeline import Stage
from imbue.mngr_mapreduce.pipeline import Step

# Geometry, in px. Every element is drawn at its own origin and stacked with a translate,
# so only widths and x positions are absolute.
_WIDTH: Final[int] = 960
_MARGIN: Final[int] = 32
_CARD_X: Final[int] = 40
_CARD_WIDTH: Final[int] = _WIDTH - 2 * _CARD_X
_CARD_RADIUS: Final[int] = 6
_TERMINAL_WIDTH: Final[int] = 680
_TERMINAL_RADIUS: Final[int] = 18
_PADDING: Final[int] = 18
_STEPS_COLUMN_WIDTH: Final[int] = 250
_COLUMN_GAP: Final[int] = 24
_LINE_HEIGHT: Final[int] = 17
_ENTRY_GAP: Final[int] = 7
_LABEL_HEIGHT: Final[int] = 14
_CONTINUATION_INDENT: Final[int] = 14
_NAME_GAP: Final[int] = 8
_SPAN_GAP: Final[int] = 6
_BASELINE_OFFSET: Final[int] = 13
_TITLE_BASELINE_OFFSET: Final[int] = 15
_TITLE_BLOCK_HEIGHT: Final[int] = 28
_BADGE_GAP: Final[int] = 12
_PILL_HEIGHT: Final[int] = 20
_PILL_PADDING_X: Final[int] = 8
_PILL_GAP: Final[int] = 8
_PILL_ROW_GAP: Final[int] = 6
_ARROW_TEXT_GAP: Final[int] = 16
_GATES_LABEL_WIDTH: Final[int] = 44
_CONNECTOR_PADDING: Final[int] = 14
_CONNECTOR_MIN_HEIGHT: Final[int] = 48

_TITLE_FONT_SIZE: Final[float] = 15
_BODY_FONT_SIZE: Final[float] = 12.5
_LABEL_FONT_SIZE: Final[float] = 11
_PILL_FONT_SIZE: Final[float] = 11.5

# SVG can neither measure nor wrap text, so line budgets come from these per-character width
# estimates; they are generous so that text stays inside its card on every platform's fallback font.
_SANS_CHAR_WIDTH: Final[float] = 6.8
_MONO_CHAR_WIDTH: Final[float] = 7.5
_TITLE_CHAR_WIDTH: Final[float] = 9.0
_PILL_CHAR_WIDTH: Final[float] = 6.9
_BADGE_CHAR_WIDTH: Final[float] = 6.3
_MIN_FIRST_LINE_CHARS: Final[int] = 24

_SANS_FONT: Final[str] = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
_MONO_FONT: Final[str] = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

_BACKGROUND_FILL: Final[str] = "#ffffff"
_CARD_FILL: Final[str] = "#f5f7fa"
_CARD_STROKE: Final[str] = "#93a1b0"
_TERMINAL_FILL: Final[str] = "#edf2f7"
_ARROW_STROKE: Final[str] = "#4a5568"
_TEXT_STRONG: Final[str] = "#1a202c"
_TEXT_BODY: Final[str] = "#2d3748"
_TEXT_MUTED: Final[str] = "#4a5568"
_TEXT_LABEL: Final[str] = "#718096"
_BADGE_FILL: Final[str] = "#e2e8f0"
_PILL_FILL: Final[str] = "#f0f9f2"
_PILL_STROKE: Final[str] = "#2f855a"
_PILL_TEXT: Final[str] = "#22543d"

_INPUTS_TITLE: Final[str] = "inputs from the operator"
_OUTPUTS_TITLE: Final[str] = "outputs delivered to the operator"
_STEPS_LABEL: Final[str] = "steps"
_PRODUCED_BY_LABEL: Final[str] = "produced by"
_FROM_LABEL: Final[str] = "from"
_GATES_LABEL: Final[str] = "gates"
_NAME_SEPARATOR: Final[str] = ", "

_ARROW_MARKER: Final[str] = (
    "<defs>\n"
    '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
    'orient="auto-start-reverse">\n'
    f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{_ARROW_STROKE}"/>\n'
    "</marker>\n"
    "</defs>"
)


class _Fragment(FrozenModel):
    """A drawn element: its markup, drawn from y=0 downward, and the vertical space it occupies."""

    markup: str = Field(description="The SVG markup of the element")
    height: int = Field(description="The vertical space the element occupies, in px")


class _PlacedPill(FrozenModel):
    """A gate pill assigned to a row of the connector and an x position within it."""

    gate: Gate = Field(description="The gate the pill names")
    x: int = Field(description="The pill's left edge, in px")
    row: int = Field(description="The zero-based row the pill sits in")


@pure
def render_pipeline_svg(pipeline: Pipeline) -> str:
    """Draw the pipeline as a vertical flow: its inputs, one card per stage joined by gated arrows, its outputs."""
    blocks = [_terminal_node(_INPUTS_TITLE, pipeline.inputs)]
    previous_gates: tuple[Gate, ...] = ()
    for stage in pipeline.stages:
        blocks.append(_connector(previous_gates))
        blocks.append(_stage_card(stage))
        previous_gates = stage.gates
    blocks.append(_connector(previous_gates))
    blocks.append(_terminal_node(_OUTPUTS_TITLE, pipeline.outputs))

    body = _stack(blocks, 0)
    height = body.height + 2 * _MARGIN
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{_WIDTH}" height="{height}" '
            f'viewBox="0 0 {_WIDTH} {height}" font-family="{_SANS_FONT}">',
            _ARROW_MARKER,
            f'<rect x="0" y="0" width="{_WIDTH}" height="{height}" fill="{_BACKGROUND_FILL}"/>',
            _translated(body, _MARGIN),
            "</svg>",
            "",
        ]
    )


@pure
def _terminal_node(title: str, artifacts: Sequence[Artifact]) -> _Fragment:
    """The start or end of the flow: a title and each artifact with its description."""
    x = (_WIDTH - _TERMINAL_WIDTH) // 2
    inner_x = x + _PADDING
    inner_width = _TERMINAL_WIDTH - 2 * _PADDING
    entries = _stack([_artifact_entry(inner_x, inner_width, artifact) for artifact in artifacts], _ENTRY_GAP)
    entries_top = _PADDING + _TITLE_BLOCK_HEIGHT
    height = entries_top + entries.height + _PADDING
    markup = "\n".join(
        [
            _rounded_rect(x, 0, _TERMINAL_WIDTH, height, _TERMINAL_RADIUS, _TERMINAL_FILL, _CARD_STROKE, 1.5),
            _title(inner_x, title),
            _translated(entries, entries_top),
        ]
    )
    return _Fragment(markup=markup, height=height)


@pure
def _stage_card(stage: Stage) -> _Fragment:
    """The stage's name, fan-out badge, and summary above its steps and the artifacts each step produces."""
    inner_x = _CARD_X + _PADDING
    inner_width = _CARD_WIDTH - 2 * _PADDING
    artifacts_x = inner_x + _STEPS_COLUMN_WIDTH + _COLUMN_GAP
    artifacts_width = inner_width - _STEPS_COLUMN_WIDTH - _COLUMN_GAP

    badge_x = inner_x + int(len(stage.name) * _TITLE_CHAR_WIDTH) + _BADGE_GAP
    summary = _paragraph(inner_x, stage.summary, int(inner_width / _SANS_CHAR_WIDTH))
    summary_top = _PADDING + _TITLE_BLOCK_HEIGHT

    steps_column = _stack(
        [_label(inner_x, _STEPS_LABEL, ""), *[_step_entry(inner_x, step) for step in stage.steps]], _ENTRY_GAP
    )
    artifacts_column = _stack(
        [_produced_group(artifacts_x, artifacts_width, step) for step in stage.steps], _ENTRY_GAP
    )
    columns_top = summary_top + summary.height + _ENTRY_GAP
    height = columns_top + max(steps_column.height, artifacts_column.height) + _PADDING

    markup = "\n".join(
        [
            _rounded_rect(_CARD_X, 0, _CARD_WIDTH, height, _CARD_RADIUS, _CARD_FILL, _CARD_STROKE, 1.5),
            _title(inner_x, stage.name),
            _badge(badge_x, _PADDING + _TITLE_BASELINE_OFFSET - _PILL_HEIGHT + 5, stage.fanout),
            _translated(summary, summary_top),
            _translated(steps_column, columns_top),
            _translated(artifacts_column, columns_top),
        ]
    )
    return _Fragment(markup=markup, height=height)


@pure
def _connector(gates: Sequence[Gate]) -> _Fragment:
    """The arrow between two blocks, with the gates run on the upper block's branches beside it."""
    center_x = _WIDTH // 2
    pills_x = center_x + _ARROW_TEXT_GAP + _GATES_LABEL_WIDTH
    pills = _place_pills(gates, pills_x, _CARD_X + _CARD_WIDTH)
    row_count = pills[-1].row + 1 if pills else 0
    rows_height = row_count * (_PILL_HEIGHT + _PILL_ROW_GAP) - _PILL_ROW_GAP if pills else 0
    height = max(_CONNECTOR_MIN_HEIGHT, rows_height + 2 * _CONNECTOR_PADDING)
    arrow = (
        f'<line x1="{center_x}" y1="0" x2="{center_x}" y2="{height}" stroke="{_ARROW_STROKE}" '
        f'stroke-width="1.5" marker-end="url(#arrow)"/>'
    )
    if not pills:
        return _Fragment(markup=arrow, height=height)
    label = _text(
        center_x + _ARROW_TEXT_GAP,
        _CONNECTOR_PADDING + _BASELINE_OFFSET,
        _PILL_FONT_SIZE,
        [_span(_GATES_LABEL, False, _TEXT_LABEL, 400, 0)],
    )
    pill_markup = [
        _gate_pill(pill.x, _CONNECTOR_PADDING + pill.row * (_PILL_HEIGHT + _PILL_ROW_GAP), pill.gate) for pill in pills
    ]
    return _Fragment(markup="\n".join([arrow, label, *pill_markup]), height=height)


@pure
def _place_pills(gates: Sequence[Gate], first_x: int, max_x: int) -> list[_PlacedPill]:
    """Lay the gate pills out left to right, starting a new row when the next pill would cross max_x."""
    placed: list[_PlacedPill] = []
    x = first_x
    row = 0
    for gate in gates:
        width = _pill_width(gate.name)
        if x + width > max_x and x > first_x:
            row += 1
            x = first_x
        placed.append(_PlacedPill(gate=gate, x=x, row=row))
        x += width + _PILL_GAP
    return placed


@pure
def _produced_group(x: int, width: int, step: Step) -> _Fragment:
    """The names of the step's products as wrapped monospace lines, with their descriptions as a tooltip."""
    names = _NAME_SEPARATOR.join(artifact.name for artifact in step.produces)
    lines = _wrap(names, int(width / _MONO_CHAR_WIDTH))
    name_lines = [
        _text(x, _BASELINE_OFFSET + idx * _LINE_HEIGHT, _BODY_FONT_SIZE, [_span(line, True, _TEXT_STRONG, 600, 0)])
        for idx, line in enumerate(lines)
    ]
    tooltip = "\n".join(f"{artifact.name}: {artifact.description}" for artifact in step.produces)
    name_block = _Fragment(
        markup="\n".join([f"<g><title>{_escape(tooltip)}</title>", *name_lines, "</g>"]),
        height=len(lines) * _LINE_HEIGHT,
    )
    return _stack([_label(x, _PRODUCED_BY_LABEL, step.name), name_block], _ENTRY_GAP)


@pure
def _artifact_entry(x: int, width: int, artifact: Artifact) -> _Fragment:
    """The artifact's name followed by its description, wrapped to the column."""
    name_width = int(len(artifact.name) * _MONO_CHAR_WIDTH) + _NAME_GAP
    first_line_max_chars = int((width - name_width) / _SANS_CHAR_WIDTH)
    continuation_max_chars = int((width - _CONTINUATION_INDENT) / _SANS_CHAR_WIDTH)
    lines = _wrap_after_prefix(artifact.description, first_line_max_chars, continuation_max_chars)
    first_line = _text(
        x,
        _BASELINE_OFFSET,
        _BODY_FONT_SIZE,
        [_span(artifact.name, True, _TEXT_STRONG, 600, 0), _span(lines[0], False, _TEXT_MUTED, 400, _SPAN_GAP)],
    )
    continuation_lines = [
        _text(
            x + _CONTINUATION_INDENT,
            _BASELINE_OFFSET + idx * _LINE_HEIGHT,
            _BODY_FONT_SIZE,
            [_span(line, False, _TEXT_MUTED, 400, 0)],
        )
        for idx, line in enumerate(lines[1:], start=1)
    ]
    return _Fragment(markup="\n".join([first_line, *continuation_lines]), height=len(lines) * _LINE_HEIGHT)


@pure
def _step_entry(x: int, step: Step) -> _Fragment:
    """The step's name and actor, then the artifact it starts from."""
    first_line = _text(
        x,
        _BASELINE_OFFSET,
        _BODY_FONT_SIZE,
        [
            _span(step.name, True, _TEXT_STRONG, 600, 0),
            _span(_actor_label(step.actor), False, _TEXT_MUTED, 400, _SPAN_GAP),
        ],
    )
    second_line = _text(
        x,
        _BASELINE_OFFSET + _LINE_HEIGHT,
        _BODY_FONT_SIZE,
        [_span(_FROM_LABEL, False, _TEXT_MUTED, 400, 0), _span(step.base, True, _TEXT_MUTED, 400, _SPAN_GAP)],
    )
    return _Fragment(markup=f"{first_line}\n{second_line}", height=2 * _LINE_HEIGHT)


@pure
def _paragraph(x: int, text: str, max_chars: int) -> _Fragment:
    lines = _wrap(text, max_chars)
    markup = "\n".join(
        _text(x, _BASELINE_OFFSET + idx * _LINE_HEIGHT, _BODY_FONT_SIZE, [_span(line, False, _TEXT_MUTED, 400, 0)])
        for idx, line in enumerate(lines)
    )
    return _Fragment(markup=markup, height=len(lines) * _LINE_HEIGHT)


@pure
def _label(x: int, words: str, name: str) -> _Fragment:
    """A small column heading: uppercase words, optionally followed by a name in monospace."""
    spans = [_span(words.upper(), False, _TEXT_LABEL, 600, 0), _span(name, True, _TEXT_LABEL, 400, _SPAN_GAP)]
    markup = (
        f'<text x="{x}" y="{_fmt(_LABEL_FONT_SIZE)}" font-size="{_fmt(_LABEL_FONT_SIZE)}" letter-spacing="0.06em">'
        f"{''.join(spans)}</text>"
    )
    return _Fragment(markup=markup, height=_LABEL_HEIGHT)


@pure
def _title(x: int, text: str) -> str:
    return _text(x, _PADDING + _TITLE_BASELINE_OFFSET, _TITLE_FONT_SIZE, [_span(text, False, _TEXT_STRONG, 600, 0)])


@pure
def _badge(x: int, y: int, label: str) -> str:
    width = int(len(label) * _BADGE_CHAR_WIDTH) + 2 * _PILL_PADDING_X
    rect = _rounded_rect(x, y, width, _PILL_HEIGHT, _PILL_HEIGHT // 2, _BADGE_FILL, "none", 0)
    text = _text(
        x + _PILL_PADDING_X, y + _BASELINE_OFFSET + 1, _PILL_FONT_SIZE, [_span(label, False, _TEXT_BODY, 400, 0)]
    )
    return f"{rect}\n{text}"


@pure
def _gate_pill(x: int, y: int, gate: Gate) -> str:
    rect = _rounded_rect(x, y, _pill_width(gate.name), _PILL_HEIGHT, _PILL_HEIGHT // 2, _PILL_FILL, _PILL_STROKE, 1)
    text = _text(
        x + _PILL_PADDING_X, y + _BASELINE_OFFSET + 1, _PILL_FONT_SIZE, [_span(gate.name, True, _PILL_TEXT, 500, 0)]
    )
    return f"<g><title>{_escape(gate.description)}</title>\n{rect}\n{text}\n</g>"


@pure
def _pill_width(label: str) -> int:
    return int(len(label) * _PILL_CHAR_WIDTH) + 2 * _PILL_PADDING_X


@pure
def _stack(fragments: Sequence[_Fragment], gap: int) -> _Fragment:
    """Lay fragments out top to bottom, translating each below the previous one."""
    offsets = [0, *accumulate(fragment.height + gap for fragment in fragments)]
    markup = "\n".join(_translated(fragment, offset) for fragment, offset in zip(fragments, offsets, strict=False))
    height = offsets[-1] - gap if fragments else 0
    return _Fragment(markup=markup, height=height)


@pure
def _translated(fragment: _Fragment, dy: int) -> str:
    return f'<g transform="translate(0 {dy})">\n{fragment.markup}\n</g>'


@pure
def _wrap(text: str, max_chars: int) -> list[str]:
    return wrap(text, width=max_chars) or [""]


@pure
def _wrap_after_prefix(text: str, first_line_max_chars: int, continuation_max_chars: int) -> list[str]:
    """Wrap text whose first line shares its row with a prefix, or start below the prefix when little room is left."""
    if first_line_max_chars < _MIN_FIRST_LINE_CHARS:
        return ["", *_wrap(text, continuation_max_chars)]
    first_line, *rest = _wrap(text, first_line_max_chars)
    return [first_line, *wrap(" ".join(rest), width=continuation_max_chars)]


@pure
def _rounded_rect(
    x: int, y: int, width: int, height: int, radius: int, fill: str, stroke: str, stroke_width: float
) -> str:
    return (
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="{radius}" fill="{fill}" '
        f'stroke="{stroke}" stroke-width="{_fmt(stroke_width)}"/>'
    )


@pure
def _text(x: int, baseline: int, font_size: float, spans: Sequence[str]) -> str:
    return f'<text x="{x}" y="{baseline}" font-size="{_fmt(font_size)}">{"".join(spans)}</text>'


@pure
def _span(text: str, is_monospace: bool, fill: str, font_weight: int, dx: int) -> str:
    """One styled run of a line; dx is the gap before it, since renderers disagree on spaces between runs."""
    family = f' font-family="{_MONO_FONT}"' if is_monospace else ""
    shift = f' dx="{dx}"' if dx else ""
    return f'<tspan{family}{shift} font-weight="{font_weight}" fill="{fill}">{_escape(text)}</tspan>'


@pure
def _actor_label(actor: Actor) -> str:
    match actor:
        case Actor.AGENT:
            return "agent"
        case Actor.ORCHESTRATOR:
            return "orchestrator"
        case _ as unreachable:  # pragma: no cover
            assert_never(unreachable)


@pure
def _escape(text: str) -> str:
    return escape(text, {'"': "&quot;"})


@pure
def _fmt(value: float) -> str:
    return f"{value:g}"

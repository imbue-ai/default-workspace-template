"""Grok Build's tool-call labels, in the header nouns and caption verbs claude reports.

Tool names and argument keys are grok 1.0.62's own, from the tool definitions a session records
(``tool_definitions.json``). ``wait_commands_or_subagents``, ``web_fetch`` and
``web_search`` are named in the binary but absent from the measured sessions, so their labels
name no target. ``use_tool`` runs only an MCP integration tool (grok refuses a native one through
it), so it reads as the tool it calls. A tool not in the table keeps grok's own label.
"""

from enum import auto
from typing import Any
from typing import Final
from typing import assert_never

from pydantic import Field

from imbue.chat.harnesses.tool_labels import GENERIC_CAPTION
from imbue.chat.harnesses.tool_labels import basename
from imbue.chat.harnesses.tool_labels import first_string_value
from imbue.chat.harnesses.tool_labels import mcp_caption
from imbue.chat.harnesses.tool_labels import parse_input_preview
from imbue.chat.harnesses.tool_labels import quoted
from imbue.chat.harnesses.tool_labels import shorten
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure


class TargetStyle(UpperCaseStrEnum):
    """How a call's target reads after the caption's verb."""

    BASENAME = auto()
    QUOTED = auto()
    SHORTENED = auto()


class GrokToolLabel(FrozenModel):
    """The shared-vocabulary labels of one grok tool."""

    noun: str = Field(description="The header's tool noun, as claude names the same tool")
    verb: str = Field(description="The caption's verb")
    target_keys: tuple[str, ...] = Field(description="Argument keys naming the call's target, most telling first")
    target_style: TargetStyle = Field(description="How the target reads in the caption")


TOOL_LABELS: Final[dict[str, GrokToolLabel]] = {
    "run_terminal_command": GrokToolLabel(
        noun="Bash", verb="Running", target_keys=("description", "command"), target_style=TargetStyle.SHORTENED
    ),
    "monitor": GrokToolLabel(
        noun="Monitor", verb="Monitoring", target_keys=("description", "command"), target_style=TargetStyle.SHORTENED
    ),
    "read_file": GrokToolLabel(
        noun="Read", verb="Reading", target_keys=("target_file",), target_style=TargetStyle.BASENAME
    ),
    "write": GrokToolLabel(
        noun="Write", verb="Writing", target_keys=("file_path",), target_style=TargetStyle.BASENAME
    ),
    "search_replace": GrokToolLabel(
        noun="Edit", verb="Editing", target_keys=("file_path",), target_style=TargetStyle.BASENAME
    ),
    "grep": GrokToolLabel(noun="Grep", verb="Searching", target_keys=("pattern",), target_style=TargetStyle.QUOTED),
    "list_dir": GrokToolLabel(
        noun="List", verb="Listing", target_keys=("target_directory",), target_style=TargetStyle.BASENAME
    ),
    "search_tool": GrokToolLabel(
        noun="ToolSearch", verb="Loading tool", target_keys=("query",), target_style=TargetStyle.QUOTED
    ),
    "get_command_or_subagent_output": GrokToolLabel(
        noun="TaskOutput", verb="Retrieving results", target_keys=(), target_style=TargetStyle.SHORTENED
    ),
    "wait_commands_or_subagents": GrokToolLabel(
        noun="TaskOutput", verb="Waiting for background work", target_keys=(), target_style=TargetStyle.SHORTENED
    ),
    "kill_command_or_subagent": GrokToolLabel(
        noun="TaskStop", verb="Stopping a background task", target_keys=(), target_style=TargetStyle.SHORTENED
    ),
    "web_search": GrokToolLabel(
        noun="WebSearch", verb="Searching the web", target_keys=(), target_style=TargetStyle.SHORTENED
    ),
    "web_fetch": GrokToolLabel(
        noun="WebFetch", verb="Fetching page", target_keys=(), target_style=TargetStyle.SHORTENED
    ),
    "scheduler_create": GrokToolLabel(
        noun="Schedule", verb="Scheduling", target_keys=("prompt",), target_style=TargetStyle.QUOTED
    ),
    "scheduler_list": GrokToolLabel(
        noun="Schedule", verb="Listing scheduled tasks", target_keys=(), target_style=TargetStyle.SHORTENED
    ),
    "scheduler_delete": GrokToolLabel(
        noun="Schedule", verb="Cancelling a scheduled task", target_keys=(), target_style=TargetStyle.SHORTENED
    ),
}

_SUBAGENT_TOOL_NAME: Final[str] = "spawn_subagent"
_SUBAGENT_HEADER: Final[str] = "Tool: Agent"
_SUBAGENT_CAPTION: Final[str] = "Delegating to sub-agent…"

_USE_TOOL_NAME: Final[str] = "use_tool"
USE_TOOL_INNER_NAME_KEY: Final[str] = "tool_name"

SHELL_TOOL_NAMES: Final[frozenset[str]] = frozenset({"run_terminal_command", "monitor"})
SHELL_COMMAND_KEY: Final[str] = "command"


@pure
def _render_target(value: str, style: TargetStyle) -> str:
    match style:
        case TargetStyle.BASENAME:
            return basename(value)
        case TargetStyle.QUOTED:
            return quoted(value)
        case TargetStyle.SHORTENED:
            return shorten(value)
        case _ as unreachable:
            assert_never(unreachable)


@pure
def _caption(label: GrokToolLabel, tool_input: dict[str, Any]) -> str:
    if not label.target_keys:
        return label.verb
    target = first_string_value(tool_input, *label.target_keys)
    if target is None:
        return f"{label.verb}…"
    return f"{label.verb} {_render_target(target, label.target_style)}"


@pure
def _use_tool_labels(tool_input: dict[str, Any], native_label: str) -> tuple[str, str]:
    inner_name = first_string_value(tool_input, USE_TOOL_INNER_NAME_KEY)
    if inner_name is None:
        return f"Tool: {_USE_TOOL_NAME}", native_label or GENERIC_CAPTION
    return f"Tool: {inner_name}", mcp_caption(inner_name) or f"Running {shorten(inner_name)}"


@pure
def tool_labels(tool_name: str, raw_input_json: str, native_label: str) -> tuple[str, str]:
    """``(header_label, caption_label)`` for one grok tool call.

    ``raw_input_json`` is the call's ``rawInput`` as JSON; ``native_label`` is grok's own
    ``_meta["x.ai/tool"].label`` (``Run Command``, ``Search``), the caption of a tool the table
    does not name.
    """
    if tool_name == _SUBAGENT_TOOL_NAME:
        return _SUBAGENT_HEADER, _SUBAGENT_CAPTION
    tool_input = parse_input_preview(raw_input_json)
    if tool_name == _USE_TOOL_NAME:
        return _use_tool_labels(tool_input, native_label)
    label = TOOL_LABELS.get(tool_name)
    if label is None:
        header = f"Tool: {tool_name}" if tool_name else "Tool"
        return header, native_label or GENERIC_CAPTION
    return f"Tool: {label.noun}", _caption(label, tool_input)


@pure
def shell_command(tool_name: str, raw_input_json: str) -> str | None:
    """The shell command this call runs, or None for a call that runs none.

    ``run_terminal_command`` and ``monitor`` both run their ``command``. A ``use_tool`` call runs
    none: grok refuses a native tool through it.
    """
    if tool_name not in SHELL_TOOL_NAMES:
        return None
    return first_string_value(parse_input_preview(raw_input_json), SHELL_COMMAND_KEY)

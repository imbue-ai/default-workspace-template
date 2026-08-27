"""Claude's tool-call labels: ``Tool: Read`` for the header, ``Reading foo.py`` for the caption.

Claude reports the real tool name on every call, so the header is simply that
name -- no translation table needed. Only the caption's verb and target are
derived. The codex peer is :mod:`tool_labels`.
"""

from typing import Any

from imbue.imbue_common.pure import pure
from imbue.system_interface.harnesses.tool_labels import GENERIC_CAPTION
from imbue.system_interface.harnesses.tool_labels import basename
from imbue.system_interface.harnesses.tool_labels import first_string_value
from imbue.system_interface.harnesses.tool_labels import mcp_caption
from imbue.system_interface.harnesses.tool_labels import parse_input_preview
from imbue.system_interface.harnesses.tool_labels import quoted
from imbue.system_interface.harnesses.tool_labels import shorten
from imbue.system_interface.harnesses.tool_output import is_tk_lifecycle_anywhere

_BASH_TOOL_NAME = "Bash"

# Agent / Task are handled before this table -- they caption as a delegation
# rather than as a verb over a target.
_VERB_BY_TOOL_NAME: dict[str, str] = {
    "Read": "Reading",
    "Edit": "Editing",
    "MultiEdit": "Editing",
    "Write": "Writing",
    "Bash": "Running",
    "Grep": "Searching",
    "Glob": "Searching",
    "Skill": "Loading skill",
    "ToolSearch": "Loading tool",
    "WebSearch": "Searching the web",
    "WebFetch": "Fetching page",
    "LSP": "Querying language server",
    "NotebookEdit": "Editing notebook",
    "Monitor": "Monitoring",
    "SendMessage": "Sending message",
}

_SUBAGENT_TOOL_NAMES = ("Agent", "Task")
_SUBAGENT_CAPTION = "Delegating to sub-agent…"

# Input keys that can name what a call is acting on, most specific first: a Read
# has a file_path, a Grep has a pattern, an unrecognised tool may only have a
# description. Order is load-bearing -- a WebFetch has both url and description.
_TARGET_PATH_KEYS = ("file_path", "path")
_TARGET_TEXT_KEYS = ("url", "command")
_TARGET_QUOTED_KEYS = ("pattern", "query")
_TARGET_PLAIN_KEYS = ("skill", "description")


@pure
def _target(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """What the call is acting on, as it should read after the verb."""
    # Bash: the agent's own description says what the command is FOR, which reads
    # far better than a shell line that the preview may have clipped mid-word.
    if tool_name == "Bash":
        described = first_string_value(tool_input, "description", "command")
        return shorten(described) if described is not None else None

    path = first_string_value(tool_input, *_TARGET_PATH_KEYS)
    if path is not None:
        return basename(path)
    text = first_string_value(tool_input, *_TARGET_TEXT_KEYS)
    if text is not None:
        return shorten(text)
    searched = first_string_value(tool_input, *_TARGET_QUOTED_KEYS)
    if searched is not None:
        return quoted(searched)
    plain = first_string_value(tool_input, *_TARGET_PLAIN_KEYS)
    if plain is not None:
        return shorten(plain)
    return None


@pure
def tool_labels(tool_name: str, input_preview: str) -> tuple[str, str]:
    """``(header_label, caption_label)`` for one claude tool call."""
    header_label = f"Tool: {tool_name}" if tool_name else "Tool"

    if tool_name in _SUBAGENT_TOOL_NAMES:
        return header_label, _SUBAGENT_CAPTION

    tool_input = parse_input_preview(input_preview)
    verb = _VERB_BY_TOOL_NAME.get(tool_name)
    target = _target(tool_name, tool_input)

    if verb is not None:
        return header_label, f"{verb} {target}" if target is not None else f"{verb}…"

    mcp = mcp_caption(tool_name)
    if mcp is not None:
        return header_label, mcp

    if target is not None:
        return header_label, f"Running {target}"
    return header_label, GENERIC_CAPTION


@pure
def shell_command(tool_name: str, raw_input: str) -> str | None:
    """The shell command this tool call runs, or None if it is not a shell call.

    The ONE question each harness answers for itself. Whether that command is a tk lifecycle
    invocation is decided centrally (``tool_output.is_pure_tk_lifecycle_command`` for the hide
    rule, ``is_tk_lifecycle_anywhere`` for the truncation exemption), so the rules live in one
    place and cannot drift between harnesses.
    """
    if tool_name != _BASH_TOOL_NAME:
        return None
    command = parse_input_preview(raw_input).get("command")
    return command if isinstance(command, str) else None


@pure
def keeps_full_tool_input(tool_name: str, raw_input: str) -> bool:
    """True when a tool call's stored input must NOT be truncated for display: a tk lifecycle
    command, whose ``--step`` titles and close summaries the step progress view reads out of
    the command itself. Moved here from ``session_parser``, which no longer knows claude's
    tool names."""
    command = shell_command(tool_name, raw_input)
    return command is not None and is_tk_lifecycle_anywhere(command)

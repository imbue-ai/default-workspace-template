"""opencode's tool-call labels: ``Tool: Read`` for the header, ``Reading foo.py`` for the caption.

opencode reports the real tool id on every ``tool`` part (``read`` / ``bash`` / ``edit`` / ...),
so -- like pi and claude, unlike codex's code-mode ``exec`` -- the header is just that name
title-cased and only the caption's verb + target is derived. This mirrors :mod:`pi_coding.tool_labels`
for opencode's lowercase tool ids and argument shapes.

Vocabulary + argument keys verified live (opencode 1.18.15): an agent was driven through each
built-in tool and the actual ``state.input`` read out of its ``opencode.db`` -- ``read {filePath}``,
``bash {command}`` (NO ``description`` field, unlike claude), ``grep {pattern}``, ``glob {pattern}``,
``edit {filePath,...}``, ``write {filePath,...}``, ``list {path}``, ``webfetch {url}``,
``websearch {query}``, ``skill {name}``. MCP tools arrive as ``<server>_<toolname>`` (a single
underscore; a tool named ``default`` registers as just ``<server>``) -- NOT the ``mcp__server__tool``
form the shared ``mcp_caption`` matches, and there is no reliable way to re-split it (server names
and built-ins like ``apply_patch`` both contain underscores) -- so an MCP tool falls to the honest
``Tool: <id>`` + generic caption. Re-confirm when OPENCODE_VERSION moves.
"""

from typing import Any

from tk_command_parsing.parser import parse_command

from imbue.imbue_common.pure import pure
from imbue.system_interface.harnesses.tool_labels import GENERIC_CAPTION
from imbue.system_interface.harnesses.tool_labels import basename
from imbue.system_interface.harnesses.tool_labels import first_string_value
from imbue.system_interface.harnesses.tool_labels import parse_input_preview
from imbue.system_interface.harnesses.tool_labels import quoted
from imbue.system_interface.harnesses.tool_labels import shorten

# tk lifecycle verbs whose Bash command must survive input truncation, so the chat progress
# view can read the ``--step`` titles / close summaries out of the command (mirrors the
# pi/claude/codex parsers' set).
_TK_LIFECYCLE_VERBS = frozenset({"create", "start", "close"})
_BASH_TOOL_NAME = "bash"

# opencode's tool ids are lowercase; title-case them for the header so it reads like claude's
# ("Tool: Read"). Where opencode has a claude equivalent the noun matches it so the two harnesses
# read alike. A tool absent here falls back to its raw id (so an MCP tool reads ``Tool: server_x``).
_HEADER_NOUN_BY_TOOL: dict[str, str] = {
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "patch": "Edit",
    "apply_patch": "Edit",
    "multiedit": "Edit",
    "bash": "Bash",
    "grep": "Grep",
    "glob": "Glob",
    "list": "List",
    "webfetch": "WebFetch",
    "websearch": "WebSearch",
    "task": "Task",
    "skill": "Skill",
    "todowrite": "TodoWrite",
    "todoread": "TodoRead",
}

# Caption verb per tool. Absent -> the generic path ("Running <target>" / fallback). ``task`` is
# handled before this table (it captions as a delegation, not a verb + target).
_VERB_BY_TOOL_NAME: dict[str, str] = {
    "read": "Reading",
    "write": "Writing",
    "edit": "Editing",
    "patch": "Editing",
    "apply_patch": "Editing",
    "multiedit": "Editing",
    "bash": "Running",
    "grep": "Searching",
    "glob": "Searching",
    "list": "Listing",
    "webfetch": "Fetching page",
    "websearch": "Searching the web",
    "skill": "Loading skill",
    "todowrite": "Updating todos",
    "todoread": "Reading todos",
}

# opencode's task tool spawns a subagent session; captioned as a delegation. (We run opencode
# with subagents disabled, so this is defensive -- but kept for parity with claude/pi.)
_SUBAGENT_TOOL_NAME = "task"
_SUBAGENT_CAPTION = "Delegating to sub-agent…"

# Input keys that name what a call acts on, most specific first. opencode uses camelCase
# ``filePath`` (verified) for the file tools, ``path`` for ``list``.
_TARGET_PATH_KEYS = ("filePath", "path")
# opencode's ``bash`` has only ``command`` (no model-written ``description``, verified), so the
# command itself is the target -- like pi's bash, unlike claude's.
_TARGET_TEXT_KEYS = ("command", "url")
_TARGET_QUOTED_KEYS = ("pattern", "query")
_TARGET_PLAIN_KEYS = ("name",)


@pure
def _target(tool_input: dict[str, Any]) -> str | None:
    """What the call is acting on, as it should read after the verb (mirrors pi/claude order)."""
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
    """``(header_label, caption_label)`` for one opencode tool call."""
    header_noun = _HEADER_NOUN_BY_TOOL.get(tool_name, tool_name)
    header_label = f"Tool: {header_noun}" if header_noun else "Tool"

    if tool_name == _SUBAGENT_TOOL_NAME:
        return header_label, _SUBAGENT_CAPTION

    tool_input = parse_input_preview(input_preview)
    verb = _VERB_BY_TOOL_NAME.get(tool_name)
    target = _target(tool_input)

    if verb is not None:
        return header_label, f"{verb} {target}" if target is not None else f"{verb}…"
    if target is not None:
        return header_label, f"Running {target}"
    return header_label, GENERIC_CAPTION


@pure
def keeps_full_tool_input(tool_name: str, raw_input: str) -> bool:
    """True when an opencode tool call's input must NOT be truncated for display.

    A ``bash`` call running a ``tk`` lifecycle command (``tk create|start|close``): the step
    timeline reads its ``--step`` titles and close summaries out of the command, so a mid-body
    clip would truncate the plan. Recognition uses the shared ``tk_command_parsing`` shlex parser
    (as the pi/claude/codex parsers do), so a ``tk close`` merely mentioned inside another
    command's quoted argument is not mistaken for a real lifecycle call. ``raw_input`` is the
    untruncated ``{"command": ...}`` JSON of the tool's ``state.input``.
    """
    if tool_name != _BASH_TOOL_NAME:
        return False
    command = parse_input_preview(raw_input).get("command")
    if not isinstance(command, str):
        return False
    parsed = parse_command(command)
    if parsed is None:
        return False
    return any(segment.tk_verb in _TK_LIFECYCLE_VERBS for segment in parsed.segments)

"""pi's tool-call labels: ``Tool: Read`` for the header, ``Reading foo.py`` for the caption.

pi reports the real tool name on every call (``read`` / ``bash`` / ``edit`` / ...),
so -- like claude, unlike codex's code-mode ``exec`` -- the header is just that name
title-cased and only the caption's verb + target is derived. The claude peer is
:mod:`harnesses.claude.tool_labels`; this mirrors it for pi's lowercase tool names
and argument shapes (verified live: ``read {path,limit}``, ``bash {command}``, ...).
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

# pi's built-in tool names are lowercase; title-case them for the header so it reads
# like claude's ("Tool: Read"). A tool absent from this table falls back to its raw name.
#
# The web_* entries are the pi-web-access extension (npm:pi-web-access), not built-ins.
# Their header nouns match claude's WebSearch/WebFetch (and codex's web__run -> WebSearch)
# so the three harnesses read alike. Names verified live against pi-web-access 0.19.0:
# web_search {query|queries}, fetch_content {url|urls}, source_check {claim},
# get_search_content {responseId,...}. These are the package's default tool names; a
# web-search.json `toolNames` override would rename them and skip this table.
_HEADER_NOUN_BY_TOOL: dict[str, str] = {
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "bash": "Bash",
    "grep": "Grep",
    "find": "Find",
    "ls": "List",
    "web_search": "WebSearch",
    "fetch_content": "WebFetch",
    "source_check": "SourceCheck",
    "get_search_content": "SearchContent",
}

# Caption verb per tool. Absent -> the generic path (mcp / "Running <target>" / fallback).
_VERB_BY_TOOL_NAME: dict[str, str] = {
    "read": "Reading",
    "write": "Writing",
    "edit": "Editing",
    "bash": "Running",
    "grep": "Searching",
    "find": "Searching",
    "ls": "Listing",
    "web_search": "Searching the web",
    "fetch_content": "Fetching page",
    "source_check": "Checking sources",
    "get_search_content": "Retrieving results",
}

# Input keys that name what a call acts on, most specific first (mirrors claude's order).
# ``url`` covers fetch_content; ``query`` covers web_search / get_search_content; ``claim``
# covers source_check's assertion (read like claude's plain ``description`` target).
_TARGET_PATH_KEYS = ("file_path", "path")
_TARGET_TEXT_KEYS = ("command", "url")
_TARGET_QUOTED_KEYS = ("pattern", "query")
_TARGET_PLAIN_KEYS = ("claim", "description")

# tk lifecycle verbs whose Bash command must survive input truncation, so the chat
# progress view can read the ``--step`` titles / close summaries out of the command
# (mirrors the claude/codex parsers' set).

# pi's shell tool -- the one whose command can be a tk lifecycle op.
_BASH_TOOL_NAME = "bash"


@pure
def _target(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """What the call is acting on, as it should read after the verb.

    Unlike claude's Bash (which carries a model-written ``description``), pi's ``bash``
    call has only ``command``, so the command itself is the target.
    """
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
    """``(header_label, caption_label)`` for one pi tool call."""
    header_noun = _HEADER_NOUN_BY_TOOL.get(tool_name, tool_name)
    header_label = f"Tool: {header_noun}" if header_noun else "Tool"

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
    rule, ``is_tk_lifecycle_anywhere`` for the resident tk_command stamp), so the rules live in one
    place and cannot drift between harnesses.
    """
    if tool_name != _BASH_TOOL_NAME:
        return None
    command = parse_input_preview(raw_input).get("command")
    return command if isinstance(command, str) else None

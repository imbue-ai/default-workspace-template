"""Claude's tool-call labels: ``Tool: Read`` for the header, ``Reading foo.py`` for the caption.

Claude reports the real tool name on every call, so the header is simply that
name -- no translation table needed. Only the caption's verb and target are
derived. The codex peer is :mod:`tool_labels`.
"""

from typing import Any

from imbue.chat.harnesses.tool_labels import GENERIC_CAPTION
from imbue.chat.harnesses.tool_labels import basename
from imbue.chat.harnesses.tool_labels import first_string_value
from imbue.chat.harnesses.tool_labels import mcp_caption
from imbue.chat.harnesses.tool_labels import parse_input_preview
from imbue.chat.harnesses.tool_labels import quoted
from imbue.chat.harnesses.tool_labels import past_tense
from imbue.chat.harnesses.tool_labels import shorten
from imbue.chat.harnesses.tool_labels import shorten_path
from imbue.chat.harnesses.tool_labels import stated_note
from imbue.imbue_common.pure import pure

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


# The tools whose target is what was looked FOR rather than where. "searched
# system/apps/chat" says nothing about what was being sought.
_SEARCH_TOOL_NAMES = ("Grep", "Glob", "WebSearch")


@pure
def _action_target(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """What the call acted on, for a chip: the literal thing, not a summary.

    This and the caption's ``_target`` diverge on exactly one tool, deliberately.
    For a shell call the caption prefers the agent's own description, because the
    live strip has room for one phrase and "why" beats "what". A chip shows that
    description separately (as the note), so here the shell target is the command
    that actually ran -- otherwise the two halves of a Bash chip would say the
    same thing twice.
    """
    if tool_name == _BASH_TOOL_NAME:
        command = first_string_value(tool_input, "command")
        return shorten(command) if command is not None else None

    if tool_name in _SEARCH_TOOL_NAMES:
        searched = first_string_value(tool_input, *_TARGET_QUOTED_KEYS)
        if searched is not None:
            scope = first_string_value(tool_input, *_TARGET_PATH_KEYS)
            return f"{quoted(searched)} in {shorten_path(scope)}" if scope is not None else quoted(searched)

    path = first_string_value(tool_input, *_TARGET_PATH_KEYS)
    if path is not None:
        return shorten_path(path)
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
def action_parts(tool_name: str, input_preview: str) -> tuple[str, str]:
    """``(verb, target)`` for a tool chip -- what this call did, in past tense.

    Returned as two halves rather than one string because the chip sets them in
    different type: the verb is prose, the target is the machine's own text.
    ``target`` is empty when the call acted on nothing nameable.

    A tool with no verb in the table falls back to its own name as the verb --
    the one case where a chip still says which tool ran, because there is nothing
    more informative to say about it.
    """
    if not tool_name:
        return "ran a tool", ""
    tool_input = parse_input_preview(input_preview)
    if tool_name in _SUBAGENT_TOOL_NAMES:
        delegated = first_string_value(tool_input, "description")
        return ("delegated", shorten(delegated)) if delegated is not None else ("delegated to a sub-agent", "")

    target = _action_target(tool_name, tool_input) or ""
    participle = _VERB_BY_TOOL_NAME.get(tool_name)
    if participle is not None:
        return past_tense(participle), target

    mcp = mcp_caption(tool_name)
    if mcp is not None:
        # "Running <tool with spaces>" -> "called <tool with spaces>".
        return f"called {mcp.removeprefix('Running ')}", target
    return tool_name, target


@pure
def action_note(tool_name: str, input_preview: str) -> str | None:
    """The agent's own words for this call, or None when it wrote none.

    Only claude's shell and delegation tools take a description; a read, edit or
    write records nothing of the kind, and this returns None for them rather than
    inventing something.
    """
    if tool_name != _BASH_TOOL_NAME and tool_name not in _SUBAGENT_TOOL_NAMES:
        return None
    return stated_note(parse_input_preview(input_preview))


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
    rule, ``is_tk_lifecycle_anywhere`` for the resident tk_command stamp), so the rules live in one
    place and cannot drift between harnesses.
    """
    if tool_name != _BASH_TOOL_NAME:
        return None
    command = parse_input_preview(raw_input).get("command")
    return command if isinstance(command, str) else None

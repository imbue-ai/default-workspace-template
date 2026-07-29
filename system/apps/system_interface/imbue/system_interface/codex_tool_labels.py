"""Codex's tool-call labels. The claude peer is :mod:`claude_tool_labels`.

Codex runs in code mode (pinned via ``features.code_mode_host``), so nearly every
operation arrives as one ``exec`` tool whose input is a JavaScript program calling
``tools.<fn>({...})``. ``Tool: exec`` would therefore be a useless header, so the
header is derived from the inner function too -- which means codex needs the
translation table claude does not: ``apply_patch`` -> ``Tool: Edit``.

The JS is arbitrary, so only the function NAME is parsed for certain (always a
literal identifier). The target is best-effort, read only out of a plain string
literal. Anything unrecognised falls back to "Running code" rather than guessing.

Tool set confirmed against a live Minds codex agent via ``ALL_TOOLS``. Two tools
are deliberately absent: ``update_plan`` and ``request_user_input`` are forbidden
by the codex prompt, so they fall to the generic label -- if one ever surfaces in
a transcript, that IS the signal the ban leaked. The ``collaboration.*`` tools are
absent for the same reason as ``features.multi_agent_v2 = false``: a spawned agent
writes its own rollout, which this workspace's watcher never opens.
"""

import re

from imbue.imbue_common.pure import pure
from imbue.system_interface.tool_labels import GENERIC_CAPTION
from imbue.system_interface.tool_labels import basename
from imbue.system_interface.tool_labels import first_string_value
from imbue.system_interface.tool_labels import mcp_caption
from imbue.system_interface.tool_labels import parse_input_preview
from imbue.system_interface.tool_labels import quoted
from imbue.system_interface.tool_labels import shorten

# The code-mode wrapper: its inner tools.<fn> supplies both labels.
CODE_MODE_TOOL_NAME = "exec"
# Top-level tool used to poll a yielded async cell -- not a tools.<fn>.
_WAIT_TOOL_NAME = "wait"

_UNKNOWN_CODE_HEADER = "Tool: Code"
_UNKNOWN_CODE_CAPTION = "Running code"

# tools.<fn> -> (header noun, caption verb). The nouns are ours, not codex's:
# code mode reports only "exec", so a readable header has to be translated. Where
# claude has an equivalent tool the name matches it (Edit, Bash, WebSearch) so the
# two harnesses read alike; the rest are named in the same style.
_LABELS_BY_FUNCTION: dict[str, tuple[str, str]] = {
    "apply_patch": ("Edit", "Editing"),
    "exec_command": ("Bash", "Running"),
    "web__run": ("WebSearch", "Searching the web"),
    "view_image": ("ViewImage", "Viewing image"),
    "write_stdin": ("WriteStdin", "Typing into terminal"),
    "create_goal": ("CreateGoal", "Setting a goal"),
    "get_goal": ("GetGoal", "Checking the goal"),
    "update_goal": ("UpdateGoal", "Updating the goal"),
}

_CODE_MODE_CALL_RE = re.compile(r"tools\.([A-Za-z_]\w*)\s*\(")
# The patch header sits inside a JS string literal, so it ends at the closing
# quote or at the literal ``\n`` escape between lines.
_APPLY_PATCH_HEADER_RE = re.compile(r"\*\*\*\s+(?:Add|Update|Delete) File:\s*([^\"\\]+)", re.IGNORECASE)


@pure
def _first_js_string_argument(js: str, *keys: str) -> str | None:
    """The first ``"key": "value"`` string literal found, in the order given.

    Codex serialises the arguments as JSON, but the surrounding program is
    arbitrary JS, so this reads the literal rather than parsing the whole thing.
    """
    for key in keys:
        match = re.search(rf'["\']?{re.escape(key)}["\']?\s*:\s*"([^"]*)"', js)
        if match:
            return match.group(1)
    return None


@pure
def _target_for_function(function_name: str, js: str) -> str | None:
    """Best-effort target for a ``tools.<fn>`` call; None when it is not a literal."""
    if function_name == "apply_patch":
        match = _APPLY_PATCH_HEADER_RE.search(js)
        return basename(match.group(1).strip()) if match else None
    if function_name == "exec_command":
        command = _first_js_string_argument(js, "cmd", "command")
        return shorten(command) if command is not None else None
    if function_name == "web__run":
        query = _first_js_string_argument(js, "q", "query")
        return quoted(query) if query is not None else None
    if function_name == "view_image":
        path = _first_js_string_argument(js, "path")
        return basename(path) if path is not None else None
    return None


@pure
def codex_tool_labels(tool_name: str, input_preview: str) -> tuple[str, str]:
    """``(header_label, caption_label)`` for one codex tool call."""
    if tool_name == _WAIT_TOOL_NAME:
        return "Tool: Wait", "Waiting for code…"

    # A hosted web_search should not occur under code mode, but the parser can
    # still synthesise one from an older rollout, so label it rather than drop it.
    if tool_name == "web_search":
        query = first_string_value(parse_input_preview(input_preview), "query", "q")
        caption = f"Searching the web {quoted(query)}" if query is not None else "Searching the web"
        return "Tool: WebSearch", caption

    if tool_name != CODE_MODE_TOOL_NAME:
        mcp = mcp_caption(tool_name)
        header_label = f"Tool: {tool_name}" if tool_name else "Tool"
        return header_label, mcp if mcp is not None else GENERIC_CAPTION

    js = input_preview

    # apply_patch front-loads the (often huge) patch body into a variable, e.g.
    # `const p = "*** Begin Patch\n*** Add File: ..."; await tools.apply_patch(p)`,
    # which pushes `tools.apply_patch(` past the truncated preview -- so the scan
    # below cannot see it and the call would read "Running code". The patch header
    # sits at the START of the body, so detect it directly.
    patch_match = _APPLY_PATCH_HEADER_RE.search(js)
    if patch_match is not None:
        return "Tool: Edit", f"Editing {basename(patch_match.group(1).strip())}"

    call_match = _CODE_MODE_CALL_RE.search(js)
    if call_match is None:
        return _UNKNOWN_CODE_HEADER, _UNKNOWN_CODE_CAPTION
    function_name = call_match.group(1)

    mcp = mcp_caption(function_name)
    if mcp is not None:
        return f"Tool: {function_name}", mcp

    labels = _LABELS_BY_FUNCTION.get(function_name)
    if labels is None:
        return _UNKNOWN_CODE_HEADER, _UNKNOWN_CODE_CAPTION
    noun, verb = labels

    target = _target_for_function(function_name, js)
    caption = f"{verb} {target}" if target is not None else f"{verb}…"
    return f"Tool: {noun}", caption

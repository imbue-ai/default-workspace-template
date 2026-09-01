"""antigravity's tool-call labels. The claude peer is :mod:`tool_labels`; the codex peer is
:mod:`tool_labels`.

agy names every tool descriptively (``run_command``, ``grep_search``) and even ships its
own short caption (``metadata.f30``, e.g. ``"Running python3 showcase.py"``). But those
captions diverge in wording from the shared verb style (agy's ``"Grep search showcase.py"``
vs claude/codex's ``Searching "magic"``). To make agy read *identically* to the other
harnesses, we synthesize the labels from the shared vocabulary -- mapping each agy tool to
the same header noun (``Read``/``Edit``/``Bash``/``Grep``/``WebSearch``/…) and caption verb
claude reports and codex normalizes to -- using the shared helpers in :mod:`tool_labels`.
agy's own ``f30`` caption is the graceful fallback: for a tool we don't map (agy-only tools
like ``schedule``/``manage_task``, or a new tool in a future release) and whenever synthesis
finds no target.

Tool surface + arg-key shapes recovered from real agy 1.1.11 conversation ``.db``s; the 17
tools are listed in the docstring table of the harness spec. Re-confirm on an agy bump.
"""

from __future__ import annotations

from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.system_interface.harnesses.tool_labels import GENERIC_CAPTION
from imbue.system_interface.harnesses.tool_labels import basename
from imbue.system_interface.harnesses.tool_labels import parse_input_preview
from imbue.system_interface.harnesses.tool_labels import quoted
from imbue.system_interface.harnesses.tool_labels import shorten

# A target renderer: how a tool's target argument reads in the caption.
_BASENAME: Final[str] = "basename"
_QUOTED: Final[str] = "quoted"
_SHORTEN: Final[str] = "shorten"

# agy tool -> (header noun, caption verb, arg keys for the target, target renderer). The
# nouns/verbs are the SHARED vocabulary (what claude reports, what codex normalizes to), so
# the three harnesses read alike. Arg keys are tried in order; the first present string wins.
_LABELS: Final[dict[str, tuple[str, str, tuple[str, ...], str]]] = {
    "view_file": ("Read", "Reading", ("AbsolutePath", "TargetFile"), _BASENAME),
    "write_to_file": ("Write", "Writing", ("TargetFile",), _BASENAME),
    "replace_file_content": ("Edit", "Editing", ("TargetFile",), _BASENAME),
    "multi_replace_file_content": ("Edit", "Editing", ("TargetFile",), _BASENAME),
    "grep_search": ("Grep", "Searching", ("Query",), _QUOTED),
    "list_dir": ("List", "Listing", ("DirectoryPath",), _BASENAME),
    "run_command": ("Bash", "Running", ("CommandLine",), _SHORTEN),
    "search_web": ("WebSearch", "Searching the web", ("Query",), _QUOTED),
    "read_url_content": ("WebFetch", "Fetching", ("Url", "URL"), _SHORTEN),
    "generate_image": ("ImageGen", "Generating an image", ("Prompt", "ImageName"), _QUOTED),
    # The remainder of agy's declared tool set. Captions stay in the shared vocabulary where a
    # claude equivalent exists (find_by_name is claude's Glob); the agy-only ones name what
    # they do. Without these the header read "Tool: manage_task" and the caption fell through
    # to the generic placeholder.
    "find_by_name": ("Glob", "Searching for", ("Pattern",), _QUOTED),
    "manage_task": ("Task", "Managing a background task", ("Action",), _QUOTED),
    "schedule": ("Schedule", "Scheduling", ("Prompt",), _QUOTED),
    "define_subagent": ("Agent", "Defining a sub-agent", ("name",), _QUOTED),
    "manage_subagents": ("Agent", "Managing sub-agents", ("Action",), _QUOTED),
    "send_message": ("Message", "Messaging a sub-agent", ("Recipient",), _QUOTED),
    "ask_question": ("Question", "Asking a question", (), _QUOTED),
}

# Subagent delegation gets the exact fixed caption claude uses for its Agent/Task tools.
_SUBAGENT_TOOL_NAMES: Final[frozenset[str]] = frozenset({"invoke_subagent"})
_SUBAGENT_HEADER: Final[str] = "Tool: Agent"
_SUBAGENT_CAPTION: Final[str] = "Delegating to sub-agent…"

_RUN_COMMAND_TOOL_NAME: Final[str] = "run_command"


@pure
def _first_string_ci(args: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """The first key's non-empty string value, matched case-insensitively. agy is
    inconsistent about arg-key casing (``grep_search`` uses ``Query`` but ``search_web``
    uses ``query``), so we normalize both sides rather than enumerate every casing."""
    lowered = {key.lower(): value for key, value in args.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if isinstance(value, str) and value:
            return value
    return None


@pure
def _render_target(value: str, renderer: str) -> str:
    if renderer == _BASENAME:
        return basename(value)
    if renderer == _QUOTED:
        return quoted(value)
    return shorten(value)


@pure
def tool_labels(tool_name: str, args_json: str, native_caption: str) -> tuple[str, str]:
    """``(header_label, caption_label)`` for one agy tool call.

    ``args_json`` is the raw (untruncated) ChatToolCall args JSON. ``native_caption`` is agy's
    own model-authored ``toolAction`` verb phrase ("Creating step", "Running test call 1 of
    20"), read from the step body; it is the fallback when we cannot synthesize a
    shared-vocabulary label. It replaces the metadata ``f30`` caption this used to read, which
    was absent on every row of both live stores measured.
    """
    if tool_name in _SUBAGENT_TOOL_NAMES:
        return _SUBAGENT_HEADER, _SUBAGENT_CAPTION

    entry = _LABELS.get(tool_name)
    if entry is None:
        # agy-only or unrecognised tool: agy's own caption reads fine; header names the tool.
        header = f"Tool: {tool_name}" if tool_name else "Tool"
        return header, (native_caption or GENERIC_CAPTION)

    noun, verb, keys, renderer = entry
    header = f"Tool: {noun}"
    args = parse_input_preview(args_json)
    target = _first_string_ci(args, keys)
    if target is not None:
        return header, f"{verb} {_render_target(target, renderer)}"
    # No parseable target: prefer agy's own caption over a bare verb.
    return header, (native_caption or f"{verb}…")


@pure
def shell_command(tool_name: str, args_json: str) -> str | None:
    """The shell command this tool call runs, or None if it is not a shell call.

    The ONE question each harness answers for itself. Whether that command is a tk lifecycle
    invocation is decided centrally (``tool_output.is_pure_tk_lifecycle_command`` for the hide
    rule, ``is_tk_lifecycle_anywhere`` for the truncation exemption), so the rules live in one
    place and cannot drift between harnesses.
    """
    if tool_name != _RUN_COMMAND_TOOL_NAME:
        return None
    return _first_string_ci(parse_input_preview(args_json), ("CommandLine",))

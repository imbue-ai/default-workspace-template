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

from typing import Final

from tk_command_parsing.parser import parse_command

from typing import Any

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
}

# Subagent delegation gets the exact fixed caption claude uses for its Agent/Task tools.
_SUBAGENT_TOOL_NAMES: Final[frozenset[str]] = frozenset({"invoke_subagent"})
_SUBAGENT_HEADER: Final[str] = "Tool: Agent"
_SUBAGENT_CAPTION: Final[str] = "Delegating to sub-agent…"

# Tools whose args carry a file body the diff view renders whole -- never truncate them.
_KEEPS_FULL_BODY_TOOLS: Final[frozenset[str]] = frozenset(
    {"write_to_file", "replace_file_content", "multi_replace_file_content"}
)
# tk lifecycle verbs whose command must survive truncation (mirrors claude/codex): a batched
# ``tk create --step`` plan / long ``tk close`` summary feeds the chat progress view.
_TK_LIFECYCLE_VERBS: Final[frozenset[str]] = frozenset({"create", "start", "close"})


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

    ``args_json`` is the raw (untruncated) ChatToolCall args JSON; ``native_caption`` is
    agy's own ``f30`` short caption, used as the fallback.
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
def keeps_full_tool_input(tool_name: str, args_json: str) -> bool:
    """True when a tool call's stored input must NOT be truncated for display: a file body
    (the diff view renders it whole) or a tk lifecycle command (the step timeline reads its
    ``--step`` titles / close summaries). Mirrors the claude/codex exemption."""
    if tool_name in _KEEPS_FULL_BODY_TOOLS:
        return True
    if tool_name == "run_command":
        command = _first_string_ci(parse_input_preview(args_json), ("CommandLine",))
        if command is not None:
            parsed = parse_command(command)
            if parsed is not None and any(segment.tk_verb in _TK_LIFECYCLE_VERBS for segment in parsed.segments):
                return True
    return False

"""Codex's tool-call labels. The claude peer is :mod:`tool_labels`.

Codex runs in code mode (pinned via ``features.code_mode_host``), so nearly every
operation arrives as one ``exec`` tool whose input is a JavaScript program calling
``tools.<fn>({...})``. ``Tool: exec`` would be a useless header, so for ``exec`` both
labels come from the inner function -- which is why codex needs a translation table
claude does not (``apply_patch`` -> ``Tool: Edit``).

Tool surface and argument shapes, from a live Minds codex agent on codex-cli 0.146.0.
Re-confirm when CODEX_VERSION moves.

    top level:
      functions.exec
      functions.wait({ cell_id: string, yield_time_ms?: number,
                       max_tokens?: number, terminate?: boolean })
      functions.request_user_input        Plan mode only; banned by our prompt

    via functions.exec:
      tools.exec_command({ cmd: "rg --files", workdir: "/home/user/workspace" })
      tools.write_stdin
      tools.apply_patch(input: string)     a backtick template literal:
          await tools.apply_patch(`*** Begin Patch
          *** Update File: path/to/file.txt
          @@
          -old line
          +new line
          *** End Patch`);
          ... also *** Add File: <path> and *** Delete File: <path>
      tools.view_image({ path: string, detail?: "high" | "original" })
      tools.image_gen__imagegen({ prompt: string,
                                  referenced_image_paths?: string[] | null,
                                  num_last_images_to_include?: number | null })
      tools.web__run({ search_query?: [{ q: string, domains?: string[], recency?: number }],
                       image_query?:  [{ q: string, domains?: string[], recency?: number }],
                       open?:       [{ ref_id: string, lineno?: number }],
                       click?:      [{ ref_id: string, id: number }],
                       find?:       [{ ref_id: string, pattern: string }],
                       screenshot?: [{ ref_id: string, pageno: number }],
                       finance?:    [{ ticker: string,
                                       type: "equity" | "fund" | "crypto" | "index",
                                       market?: string }],
                       weather?:    [{ location: string, duration?: number, start?: string }],
                       sports?:     [{ fn: "schedule" | "standings",
                                       league: "nba" | "wnba" | "nfl" | "nhl" | "mlb" | "epl"
                                                | "ncaamb" | "ncaawb" | "ipl",
                                       team?: string, opponent?: string, date_from?: string,
                                       date_to?: string, num_games?: number, locale?: string,
                                       tool?: "sports" }],
                       time?:       [{ utc_offset: string }],
                       response_length?: "short" | "medium" | "long" })
      tools.update_plan({ explanation?: string,
                          plan: [{ step: string,
                                   status: "pending" | "in_progress" | "completed" }] })
                                          banned by our prompt
      tools.create_goal                   banned by our prompt
      tools.get_goal                      banned by our prompt
      tools.update_goal({ status: "complete" | "blocked" })
                                          banned by our prompt
      tools.list_mcp_resources({ cursor?: string, server?: string })
      tools.list_mcp_resource_templates({ cursor?: string, server?: string })
      tools.read_mcp_resource({ server: string, uri: string })

    MCP server tools, also via functions.exec, 49 exposed in that session:
      tools.mcp__<server>__<function>({ ... })
      e.g. tools.mcp__codex_apps__gmail_search_emails({ query: "is:unread" })

``update_plan``, ``request_user_input``, and the three goal tools are unlabelled by design:
the codex prompt forbids all of them (they write to stores the user cannot see, competing with
``tk``), so a sighting is the signal a ban leaked. They fall to the generic label, which names
the function -- ``Tool: create_goal`` -- making the leak visible instead of dressing it up.
"""

import re

from imbue.imbue_common.pure import pure
from imbue.system_interface.harnesses.tool_labels import GENERIC_CAPTION
from imbue.system_interface.harnesses.tool_labels import basename
from imbue.system_interface.harnesses.tool_labels import mcp_caption
from imbue.system_interface.harnesses.tool_labels import quoted
from imbue.system_interface.harnesses.tool_labels import shorten
from imbue.system_interface.harnesses.tool_output import is_tk_lifecycle_anywhere

CODE_MODE_TOOL_NAME = "exec"
WAIT_TOOL_NAME = "wait"

# An exec program with no parseable tools.<fn> call. Means the JS was unparseable,
# never "no table entry".
_UNPARSEABLE_CODE_HEADER = "Tool: Code"
_UNPARSEABLE_CODE_CAPTION = "Running code"

# tools.<fn> -> (header noun, caption verb). The nouns are ours: code mode reports only
# "exec". Where claude has an equivalent tool the name matches it (Edit, Bash, WebSearch)
# so the two harnesses read alike.
_LABELS_BY_FUNCTION: dict[str, tuple[str, str]] = {
    "exec_command": ("Bash", "Running"),
    "write_stdin": ("WriteStdin", "Typing into terminal"),
    "view_image": ("ViewImage", "Viewing image"),
    "image_gen__imagegen": ("ImageGen", "Generating an image"),
    "web__run": ("WebSearch", "Searching the web"),
    "list_mcp_resources": ("ListMcpResources", "Listing MCP resources"),
    "list_mcp_resource_templates": ("ListMcpResourceTemplates", "Listing MCP resource templates"),
    "read_mcp_resource": ("ReadMcpResource", "Reading MCP resource"),
}

_CODE_MODE_CALL_RE = re.compile(r"tools\.([A-Za-z_]\w*)\s*\(")
APPLY_PATCH_FUNCTION_NAME = "apply_patch"

# apply_patch is the one function whose labels are not a fixed pair: the operation lives
# in the patch body, so `*** Add File:` reads "Creating" while `*** Update File:` reads
# "Editing". Kept out of _LABELS_BY_FUNCTION so there is one source of truth for it.
_APPLY_PATCH_LABELS: dict[str, tuple[str, str]] = {
    "add": ("Write", "Creating"),
    "update": ("Edit", "Editing"),
    "delete": ("Delete", "Deleting"),
}
# apply_patch takes a backtick template literal, so the filename ends at a real newline
# when the body arrives raw, or at the ``\n`` escape when it arrives JSON-serialised in
# ``function_call.arguments``. Stop at either, plus the closing quote.
_APPLY_PATCH_HEADER_RE = re.compile(r"\*\*\*\s+(Add|Update|Delete) File:\s*([^\"\\\r\n]+)", re.IGNORECASE)


@pure
def _js_string_argument(js: str, *keys: str) -> str | None:
    """The first ``"key": "value"`` string literal found, in the order given.

    Codex serialises the arguments as JSON, but the surrounding program is arbitrary JS,
    so this reads the literal rather than parsing the whole thing. Matching anywhere in
    the program also reaches keys nested inside an array, which is how ``web__run``
    carries its per-mode queries.
    """
    for key in keys:
        # The value pattern allows escaped chars (``\\.``) so a value containing an escaped
        # quote -- e.g. codex serialising ``cmd: "tk create --step \"Title\""`` -- is captured
        # WHOLE, not clipped at the first ``\"``. (A bare ``[^"]*`` stopped there, defeating the
        # tk-command / apply_patch recognition and mangling the exec caption.)
        match = re.search(rf'["\']?{re.escape(key)}["\']?\s*:\s*"((?:\\.|[^"\\])*)"', js)
        if match:
            return _unescape_js_string(match.group(1))
    return None


# The JS/JSON string escapes worth undoing when reading a captured value (``\"`` -> ``"``,
# ``\\`` -> ``\``, and the common whitespace escapes). An unknown escape keeps the char after
# the backslash, which is harmless for the command/caption use.
_JS_UNESCAPES: dict[str, str] = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t", "r": "\r"}


@pure
def _unescape_js_string(value: str) -> str:
    """Undo the JS/JSON string escapes in a captured value, without a decode that can raise
    (so an odd value degrades gracefully rather than being swallowed as an error)."""
    if "\\" not in value:
        return value
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            out.append(_JS_UNESCAPES.get(value[index + 1], value[index + 1]))
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


@pure
def _target_for_function(function_name: str, js: str) -> str | None:
    """The caption's target for a ``tools.<fn>`` call, from that function's arguments.

    None when the value is absent or not a string literal, which drops the caption to
    the bare verb.
    """
    if function_name == "exec_command":
        command = _js_string_argument(js, "cmd")
        return shorten(command) if command is not None else None
    if function_name == "web__run":
        # `q` under search_query / image_query; the other modes carry no free-text query.
        query = _js_string_argument(js, "q")
        return quoted(query) if query is not None else None
    if function_name == "view_image":
        path = _js_string_argument(js, "path")
        return basename(path) if path is not None else None
    if function_name == "image_gen__imagegen":
        prompt = _js_string_argument(js, "prompt")
        return quoted(prompt) if prompt is not None else None
    if function_name in ("list_mcp_resources", "list_mcp_resource_templates"):
        server = _js_string_argument(js, "server")
        return f"on {server}" if server is not None else None
    if function_name == "read_mcp_resource":
        uri = _js_string_argument(js, "uri")
        return shorten(uri) if uri is not None else None
    return None


@pure
def _apply_patch_labels(js: str) -> tuple[str, str] | None:
    """Labels for an ``apply_patch`` body, or None when it carries no file header."""
    match = _APPLY_PATCH_HEADER_RE.search(js)
    if match is None:
        return None
    noun, verb = _APPLY_PATCH_LABELS[match.group(1).lower()]
    return f"Tool: {noun}", f"{verb} {basename(match.group(2).strip())}"


@pure
def _code_mode_labels(js: str) -> tuple[str, str]:
    """Labels for an ``exec`` call, from the ``tools.<fn>`` inside its program."""
    call_match = _CODE_MODE_CALL_RE.search(js)
    if call_match is None:
        # No visible call. An apply_patch that front-loads its body into a variable --
        # `const p = "*** Begin Patch\n*** Add File: ..."; await tools.apply_patch(p)` --
        # pushes the call past the 200-char preview, leaving the header as the only
        # evidence. Anything else here is genuinely unparseable.
        return _apply_patch_labels(js) or (_UNPARSEABLE_CODE_HEADER, _UNPARSEABLE_CODE_CAPTION)
    function_name = call_match.group(1)

    if function_name == APPLY_PATCH_FUNCTION_NAME:
        # A body whose header is past the preview leaves the operation unknown; "Editing"
        # is the honest default, since Add and Delete both announce themselves early.
        return _apply_patch_labels(js) or ("Tool: Edit", "Editing…")

    mcp = mcp_caption(function_name)
    if mcp is not None:
        return f"Tool: {function_name}", mcp

    labels = _LABELS_BY_FUNCTION.get(function_name)
    if labels is None:
        return f"Tool: {function_name}", GENERIC_CAPTION
    noun, verb = labels

    target = _target_for_function(function_name, js)
    caption = f"{verb} {target}" if target is not None else f"{verb}…"
    return f"Tool: {noun}", caption


# tk lifecycle verbs whose command must survive input truncation (mirrors the claude
# parser's set): a batched `tk create --step` plan and a long `tk close` summary feed the
# chat progress view, so clipping them mid-body would truncate the plan.


@pure
def keeps_full_tool_input(tool_name: str, raw_input: str) -> bool:
    """True when a codex tool call's input must NOT be truncated for display.

    Two cases, both carried inside the code-mode ``exec`` program:
    - a patch body (``tools.apply_patch``): the diff view renders it whole, so a
      mid-body cut would truncate the diff;
    - a tk lifecycle command (``tools.exec_command`` running ``tk create|start|close``):
      the step timeline reads its ``--step`` titles / close summaries.

    tk recognition uses the shared ``tk_command_parsing`` shlex parser (as the claude
    parser does), so a ``tk close`` merely mentioned inside another command's quoted
    argument is not mistaken for a real lifecycle call.
    """
    if tool_name != CODE_MODE_TOOL_NAME:
        return False
    call_match = _CODE_MODE_CALL_RE.search(raw_input)
    function_name = call_match.group(1) if call_match is not None else None
    if function_name == APPLY_PATCH_FUNCTION_NAME:
        return True
    # An apply_patch that front-loads its body into a variable shows no visible call but
    # carries the patch header (same case _code_mode_labels handles) -- treat it as a patch.
    if function_name is None and _APPLY_PATCH_HEADER_RE.search(raw_input) is not None:
        return True
    # Segment-wise, deliberately BROADER than the hide rule (`is_tk_lifecycle`): a batched
    # `cd /code && tk create --step ...` renders as work yet its full command must survive
    # for the step timeline's input fallback -- over-preserving is harmless, over-hiding
    # is not.
    command = shell_command(tool_name, raw_input)
    return command is not None and is_tk_lifecycle_anywhere(command)


@pure
def shell_command(tool_name: str, raw_input: str) -> str | None:
    """The shell command this tool call runs, or None if it is not a shell call.

    The ONE question each harness answers for itself. Whether that command is a tk lifecycle
    invocation is decided centrally (``tool_output.is_pure_tk_lifecycle_command`` for the hide
    rule, ``is_tk_lifecycle_anywhere`` for the truncation exemption), so the rules live in one
    place and cannot drift between harnesses.

    codex runs the shell from inside code mode, so the command is an argument of an
    ``exec_command`` call in the emitted JS rather than a tool input of its own.
    """
    if tool_name != CODE_MODE_TOOL_NAME:
        return None
    call_match = _CODE_MODE_CALL_RE.search(raw_input)
    if call_match is None or call_match.group(1) != "exec_command":
        return None
    return _js_string_argument(raw_input, "cmd")


@pure
def tool_labels(tool_name: str, input_preview: str) -> tuple[str, str]:
    """``(header_label, caption_label)`` for one codex tool call.

    Under code mode the only tools codex calls directly are ``exec`` and ``wait``.
    MCP tools are not top-level: they are reached as ``tools.mcp__<server>__<fn>``
    inside an exec program, so they are handled on the code-mode path. Anything else
    arriving here is ``request_user_input`` leaking out of Plan mode, and is named.
    """
    if tool_name == CODE_MODE_TOOL_NAME:
        return _code_mode_labels(input_preview)
    if tool_name == WAIT_TOOL_NAME:
        return "Tool: Wait", "Waiting for code…"
    return (f"Tool: {tool_name}" if tool_name else "Tool"), GENERIC_CAPTION

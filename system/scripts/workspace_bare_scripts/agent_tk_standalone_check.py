"""Decide whether a Bash command mixes a `tk start`/`tk close` with anything but tk.

Takes the command as its single positional argument (passed by
agent_tk_standalone.sh). Exits 0 to allow; exits 2 with a guiding stderr
message to BLOCK. Only `start` and `close` are in scope -- `create` is exempt.
See the wrapper for the why.

A tool call may hold several tk lifecycle commands in sequence (`tk close <id>
"<summary>"; tk start <next>`, or a batch of creates followed by the first
start): the progress view applies the `Updated` lines of one output in order,
so a step boundary costs the agent one tool call instead of two. What the call
may not hold is anything else -- a leading `cd`, real work before or after the
tk command (a tk-prefixed command is hidden from the timeline, so the work
would vanish with it), a pipe, a `||`, a `&`, or a redirect on any segment.

Only a start or close that targets a step is governed. The progress view
renders steps alone and skips a regular ticket's `Updated` line, so a command
whose every start and close provably targets a regular ticket is allowed
however it is chained. A target counts as a regular ticket when it is a literal
id without the `-step-` segment, or a `$VAR` captured earlier in the same
command from `tk create` without `--step` (shell variables do not survive
between tool calls, so the capture must be in the same command). Any other
target -- a step id, a variable captured from `tk create --step`, a variable or
substitution the command does not assign -- is treated as a step.

The command structure (which segments are tk invocations, whether one is
chained or redirected) comes from the shared `tk_command_parsing` parser, which
tokenizes with `shlex` (a real shell-aware lexer) rather than matching regexes,
so quoting, escapes, comments, env-var prefixes, and operators are interpreted
the way a shell would: a `tk close` summary in quotes, or any string that merely
mentions `tk close`, stays inside one token and never trips the operator checks.

This hook runs under a bare `python3` with no virtualenv (see the wrapper), so
its entry script puts the parser lib's source directory on `sys.path` rather than
relying on an installed package; the lib is stdlib-only for the same reason.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence

from tk_command_parsing.parser import CommandSegment, parse_command

# The tk lifecycle subcommands this gate governs. `create` is exempt -- agents
# legitimately batch several `S1=$(tk create --step ...)` up front, and a create
# carries no positional transition the progress view must group around.
_LIFECYCLE_VERBS = ("start", "close")
# The tk commands that may share a tool call with a start or close.
_BATCHABLE_VERBS = ("create", "start", "close")

# A step id carries a literal `-step-` segment (`cod-step-f1zl`); a regular
# ticket id has none (`cod-f1zl`). Same pattern as STEP_ID_RE in
# system/apps/chat/frontend/src/views/turn-grouping.ts, which decides what the
# progress view renders.
_STEP_ID = re.compile(r"-step-[a-z0-9]+$")
# A literal ticket id: no quoting, expansion or substitution left in the token.
_LITERAL_ID = re.compile(r"^[A-Za-z0-9_-]+$")
# A whole-token variable reference: `$NAME` or `${NAME}`.
_VAR_REF = re.compile(r"^\$(?:([A-Za-z_]\w*)|\{([A-Za-z_]\w*)\})$")
# A leading `NAME=...` assignment word.
_ASSIGN = re.compile(r"^([A-Za-z_]\w*)=(.*)$", re.S)

_BEFORE = "another command runs before it (for example a leading `cd`)"
_REDIRECT = "its output is redirected (`>`, `>>`, `2>`, `&>`, `</dev/null`, ...)"
_CHAIN = (
    "it shares the tool call with something other than tk commands joined by `;`, `&&` or a newline "
    "(real work, a pipe, `||`, or a background `&`)"
)


def _is_sequential(terminator: str | None) -> bool:
    """True for a separator that runs the next tk command after the previous one finished.

    The parser reads an unquoted newline as `;`, and shlex joins adjacent
    operator characters into one token, so `;` or `&&` followed by a newline or
    a blank line arrives with a run of `;` appended. A pipe or a background `&`
    would detach or swallow the transition's output.
    """
    return terminator is None or terminator.rstrip(";") in ("", "&&")


def classify(cmd: str) -> str | None:
    """Return the violation reason if `cmd` mixes a tk start/close with anything but tk.

    Returns None when the command is allowed: either it is not a tk start/close
    at all (including `tk create`, or a non-tk command that merely mentions a tk
    verb inside a quoted string), or every command in it is an unredirected tk
    create/start/close run in sequence.
    """
    parsed = parse_command(cmd)
    if parsed is None:
        return None
    # A trailing `;` leaves an empty tail segment that is not a second command.
    segments = [seg for seg in parsed.segments if seg.words]

    if not any(seg.tk_verb in _LIFECYCLE_VERBS for seg in segments):
        return None
    if _targets_only_regular_tickets(segments):
        return None

    if segments[0].tk_verb not in _BATCHABLE_VERBS:
        # A tk start/close exists, but something else is the first command.
        return _BEFORE
    if any(seg.has_redirect for seg in segments):
        return _REDIRECT
    if any(seg.tk_verb not in _BATCHABLE_VERBS for seg in segments):
        # Real work shares the call with the transition.
        return _CHAIN
    if not all(_is_sequential(seg.terminator) for seg in segments):
        # A pipe, `||` or `&` between (or after) the tk commands.
        return _CHAIN
    return None


def _targets_only_regular_tickets(segments: Sequence[CommandSegment]) -> bool:
    """True when every tk start/close in `segments` provably targets a regular ticket.

    Walks the segments in order, tracking which variables hold the output of a
    `tk create` in this command. The lexer splits `ID=$(tk create ...)` into a
    segment ending in the word `ID=$` (terminated by `(`), then the create
    segment (terminated by `)`), so a capture is recognised from that pair.
    Any other assignment to a tracked variable makes its value unknown.
    """
    # Variable name -> True when it holds a step id, False for a regular ticket.
    captured: dict[str, bool] = {}
    capturing: str | None = None
    for seg in segments:
        if capturing is not None:
            terminator = seg.terminator or ""
            if seg.tk_verb == "create" and terminator.startswith(")"):
                captured[capturing] = "--step" in seg.tk_args
            capturing = None
        if seg.tk_verb in _LIFECYCLE_VERBS and not _is_regular_ticket(
            _target(seg.tk_args), captured
        ):
            return False
        for word in seg.words:
            match = _ASSIGN.match(word)
            if match is None:
                break
            captured.pop(match.group(1), None)
            if (
                match.group(2) == "$"
                and seg.terminator == "("
                and word == seg.words[-1]
            ):
                capturing = match.group(1)
    return True


def _target(args: Sequence[str]) -> str | None:
    """The ticket id a start/close names: its first argument that is not a flag."""
    return next((arg for arg in args if not arg.startswith("-")), None)


def _is_regular_ticket(target: str | None, captured: dict[str, bool]) -> bool:
    if target is None:
        return False
    var = _VAR_REF.match(target)
    if var is not None:
        return captured.get(var.group(1) or var.group(2)) is False
    return bool(_LITERAL_ID.match(target)) and not _STEP_ID.search(target)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv if argv is None else argv
    command = args[1] if len(args) > 1 else ""
    violation = classify(command)
    if violation is None:
        return 0

    sys.stderr.write(
        "Blocked: a `tk start` / `tk close` on a step may share its tool call only with other tk commands -- "
        + violation
        + ".\n\n"
        "The chat progress view reads each step's structure and grouping from this "
        "command's visible output (the `Updated <id> -> <status>` line) and its position "
        "in the transcript. A `cd` prefix, real work in the same command, or a redirect "
        "suppresses or mis-positions that, so the step stops grouping its work.\n\n"
        "tk works from any directory (it uses TICKETS_DIR), so you never need to `cd` first. "
        "Re-run with only tk commands in the call, for example:\n"
        "  tk start <id>\n"
        '  tk close <id> "<summary>"; tk start <next-id>\n'
        "and put the step's real work in its own tool call.\n"
    )
    return 2

#!/usr/bin/env python3
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

The command structure (which segments are tk invocations, whether one is
chained or redirected) comes from the shared `tk_command_parsing` parser, which
tokenizes with `shlex` (a real shell-aware lexer) rather than matching regexes,
so quoting, escapes, comments, env-var prefixes, and operators are interpreted
the way a shell would: a `tk close` summary in quotes, or any string that merely
mentions `tk close`, stays inside one token and never trips the operator checks.

This hook runs under a bare `python3` with no virtualenv (see the wrapper), so
it puts the parser lib's source directory on `sys.path` explicitly rather than
relying on an installed package; the lib is stdlib-only for the same reason.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "libs" / "tk_command_parsing" / "src")
)

from tk_command_parsing.parser import parse_command

# The tk lifecycle subcommands this gate governs. `create` is exempt -- agents
# legitimately batch several `S1=$(tk create --step ...)` up front, and a create
# carries no positional transition the progress view must group around.
_LIFECYCLE_VERBS = ("start", "close")
# The tk commands that may share a tool call with a start or close.
_BATCHABLE_VERBS = ("create", "start", "close")

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


def main(argv: list[str] | None = None) -> int:
    args = sys.argv if argv is None else argv
    command = args[1] if len(args) > 1 else ""
    violation = classify(command)
    if violation is None:
        return 0

    sys.stderr.write(
        "Blocked: a `tk start` / `tk close` may share its tool call only with other tk commands -- "
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


if __name__ == "__main__":
    sys.exit(main())

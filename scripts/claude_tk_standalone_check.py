#!/usr/bin/env python3
"""Decide whether a Bash command is a step-creation command the progress view
cannot render.

Takes the command as its single positional argument (passed by
claude_tk_standalone.sh). Exits 0 to allow; exits 2 with a guiding stderr
message to BLOCK.

Two families are in scope:

- `tk start` / `tk close` must be the ONLY command in the tool call (no leading
  `cd`, no chaining, no redirect), so the progress view always sees the
  transition's output and position. See the wrapper for the why.
- `tk create --step` may be batched (several creates in one tool call is the
  canonical up-front form), but two shapes silently break it: redirecting its
  output drops the `Created <id>: <title>` line the view reads, and passing more
  than one `--step` to a single `create` makes only ONE step (tk keeps just the
  last title). Both are blocked so the failure is loud, not silent.

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

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "libs" / "tk_command_parsing" / "src")
)

from tk_command_parsing.parser import flag_values, parse_command

# The tk lifecycle subcommands whose transition must run standalone. `create` is
# governed by its own checks below (it may be batched, but not redirected or
# given multiple `--step`s).
_LIFECYCLE_VERBS = ("start", "close")

_BEFORE = "another command runs before it (for example a leading `cd`)"
_REDIRECT = "its output is redirected (`>`, `>>`, `2>`, `&>`, `</dev/null`, ...)"
_CHAIN = "it is chained with or backgrounded by another command (`&&`, `||`, `;`, `|`, `&`, or a newline)"

# `tk create --step` shapes that silently lose steps or their rendering. A batch
# of separate `tk create` commands in one tool call stays allowed; these do not.
_CREATE_REDIRECT = "its output is redirected (`>`, `>>`, `2>`, `&>`, ...), which hides the `Created <id>: <title>` line the progress view reads"
_CREATE_MULTISTEP = "it passes more than one `--step`, but `tk create` makes only ONE step per call and silently keeps just the last title"


def classify(cmd: str) -> str | None:
    """Return the violation reason if `cmd` is a step-creation command the
    progress view cannot render, else None.

    Returns None when the command is allowed: a clean standalone start/close, a
    batch of well-formed `tk create --step` commands, or a non-tk command that
    merely mentions a tk verb inside a quoted string.
    """
    parsed = parse_command(cmd)
    if parsed is None:
        return None
    segments = parsed.segments

    # `tk create --step` may be batched, so these are checked per-segment (a
    # redirect or a repeated `--step` on any single create is the violation) --
    # not via the whole-command chaining rule that governs start/close.
    for seg in segments:
        if seg.tk_verb == "create":
            if seg.has_redirect:
                return _CREATE_REDIRECT
            if len(flag_values(seg.tk_args, "--step")) > 1:
                return _CREATE_MULTISTEP

    if not any(seg.tk_verb in _LIFECYCLE_VERBS for seg in segments):
        return None

    if segments[0].tk_verb not in _LIFECYCLE_VERBS:
        # A tk start/close exists, but something else is the first command.
        return _BEFORE
    if segments[0].has_redirect:
        return _REDIRECT
    if len(segments) > 1:
        # A control operator split the stream, so another command runs
        # alongside the tk start/close (or it is backgrounded with `&`).
        return _CHAIN
    return None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv if argv is None else argv
    command = args[1] if len(args) > 1 else ""
    violation = classify(command)
    if violation is None:
        return 0

    if violation in (_CREATE_REDIRECT, _CREATE_MULTISTEP):
        sys.stderr.write(
            "Blocked: `tk create --step` -- " + violation + ".\n\n"
            'Create each step with its own `tk create --step "..."` command. To make '
            "several at once, put them in one tool call as SEPARATE commands (on their "
            "own lines or joined with `;`) -- never several `--step`s in one `tk create` "
            "-- and do not redirect their output; the progress view reads each new step "
            "from the visible `Created <id>: <title>` line:\n"
            '  tk create --step "First step"\n'
            '  tk create --step "Second step"\n'
        )
        return 2

    sys.stderr.write(
        "Blocked: run `tk start` / `tk close` as the ONLY command in the tool call -- "
        + violation
        + ".\n\n"
        "The chat progress view reads each step's structure and grouping from this "
        "command's visible output (the `Updated <id> -> <status>` line) and its position "
        "in the transcript. Chaining the command, prefixing a `cd`, or redirecting its "
        "output suppresses or mis-positions that, so the step stops grouping its work.\n\n"
        "tk works from any directory (it uses TICKETS_DIR), so you never need to `cd` first. "
        "Re-run with just the tk command on its own:\n"
        "  tk start <id>\n"
        '  tk close <id> "<summary>"\n'
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())

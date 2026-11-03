"""Decide whether a Bash command waits in the foreground on a `sleep` of 5 seconds or more.

Takes the command as its only argument (passed by agent_block_foreground_sleep.sh, which calls
this only when the raw text contains `sleep` followed by a number or `inf`, quoted or not, and
never for a command the harness already runs in the background). Exits 0 to allow; exits 2 with
the refusal on stderr to BLOCK.

A timed sleep is a guess at how long something else takes, and it holds the agent's turn for the
whole guess: a message to the agent waits until it ends, and so does the agent when the thing it
waits on finished early. A sleep is judged wherever it runs as a command of its own -- chained
after a `cd`, alone, or in a loop or `if` body -- and is allowed when it is shorter than 5 seconds,
so a condition poll such as `until curl -sf URL; do sleep 1; done` passes. A sleep in a list the
command sends to the background with `&` (`(sleep 900 | mcpc login URL > f &)`) holds no turn and
is allowed. A sleep that is only an argument (the command handed to `run_in_background.py`, a
`bash -c` script) is not a command of this one and is never judged.

The checker reads the parser's flat list of segments, not the shell's grouping, so it is a seatbelt
with known gaps rather than a complete model:

- Blocked although backgrounded: a `;` or newline inside a backgrounded group, loop or list reads as
  the end of a foreground list (`{ sleep 30; } &`, `sleep 30 &&<newline>cmd &`), and so does a
  heredoc body that holds a sleep (the parser reads it as commands).
- Allowed although in the foreground: a sleep behind a wrapper (`timeout N`, `nohup`, `exec`,
  `env`), one whose duration is not a literal number (`sleep $DELAY`, `sleep 1e3`),
  one in a process substitution, one cleared by an inner `&` (`sleep 30 && (cmd &)`,
  `sleep 30 & wait`), a run of sleeps each under 5 seconds, and a long condition poll of short
  sleeps.

Runs under a bare `python3` with no virtualenv, like the sibling checkers, so its entry script puts
the parser lib's source directory on `sys.path`.
"""

from __future__ import annotations

import os
import re
import sys

from tk_command_parsing.parser import command_words, parse_command

_MIN_BLOCKED_SECONDS = 5.0
_DURATION = re.compile(r"^(\d*\.?\d+|(?i:inf(?:inity)?))([smhd]?)$")
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}

_REFUSAL = (
    "Do not wait with a foreground `sleep` of 5 seconds or more. To wait on a command, run it with "
    '`python3 system/scripts/workspace_bare_scripts/run_in_background.py --description "<what>" -- '
    "<command>` (or your harness's background option) and end your turn; it wakes you when done. "
    "To wait on a state, poll it with short sleeps: `until curl -sf <url>; do sleep 1; done`.\n"
)


def is_blocked(command: str) -> bool:
    """True when `command` runs a `sleep` of 5 seconds or more as one of its own commands, in a
    list it does not send to the background with `&`."""
    parsed = parse_command(command)
    if parsed is None:
        return False
    list_sleeps = False
    for segment in parsed.segments:
        words = command_words(segment)
        if words and os.path.basename(words[0]) == "sleep":
            # A redirect's fd and target stay among the words (`sleep 3 2>/dev/null` reads as
            # `sleep 3 2 /dev/null`), so a redirected sleep is judged by its first argument.
            args = words[1:2] if segment.has_redirect else words[1:]
            list_sleeps = list_sleeps or _seconds(args) >= _MIN_BLOCKED_SECONDS
        if segment.terminator is None:
            return list_sleeps
        # `|`, `&&`, `||` and a parenthesis carry the list on; `;` (a newline included) ends it
        # in the foreground, a lone `&` in the background.
        list_ends = _list_operators(segment.terminator)
        if "&" in list_ends:
            list_sleeps = False
        elif ";" in list_ends:
            if list_sleeps:
                return True
    return False


def _list_operators(terminator: str) -> str:
    """`terminator` without the operators that join commands into one list (`&&`, `||`, `|&`).

    The lexer returns a run of operators as one token (`&)`, or `&;` for a `&` before a newline),
    so what is left is tested for the operator it holds rather than compared whole.
    """
    return terminator.replace("&&", "").replace("||", "").replace("|&", "")


def _seconds(args: list[str]) -> float:
    """The total a `sleep` with these arguments waits, counting only literal durations."""
    total = 0.0
    for arg in args:
        match = _DURATION.match(arg)
        if match:
            total += float(match.group(1)) * _UNIT_SECONDS[match.group(2)]
    return total


def main(argv: list[str] | None = None) -> int:
    args = (sys.argv if argv is None else argv)[1:]
    if not is_blocked(args[0] if args else ""):
        return 0
    sys.stderr.write(_REFUSAL)
    return 2

"""Decide whether a Bash command waits in the foreground on a `sleep` of 5 seconds or more.

Takes the command as its only argument (passed by agent_block_foreground_sleep.sh, which calls
this only when the raw text contains `sleep` followed by a number, and never for a command the
harness already runs in the background). Exits 0 to allow; exits 2 with the refusal on stderr to
BLOCK.

A timed sleep is a guess at how long something else takes, and it holds the agent's turn for the
whole guess: a message to the agent waits until it ends, and so does the agent when the thing it
waits on finished early. A sleep is judged wherever it runs as a command of its own -- chained
after a `cd`, alone, or in a loop or `if` body -- and is allowed when it is shorter than 5 seconds,
so a condition poll such as `until curl -sf URL; do sleep 1; done` passes. A sleep that is only
an argument (the command handed to `run_in_background.py`, a `bash -c` script) is not a command of
this one and is never judged. Neither is a sleep whose duration is not a literal number.

Runs under a bare `python3` with no virtualenv, like the sibling checkers, so its entry script puts
the parser lib's source directory on `sys.path`.
"""

from __future__ import annotations

import os
import re
import sys

from tk_command_parsing.parser import parse_command

_MIN_BLOCKED_SECONDS = 5.0
_DURATION = re.compile(r"^(\d*\.?\d+)([smhd]?)$")
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")
_LEADING_KEYWORDS = frozenset(
    {"do", "then", "else", "elif", "if", "while", "until", "time", "!", "{"}
)

_REFUSAL = (
    "Do not wait with a foreground `sleep` of 5 seconds or more. A timed sleep is a guess at how "
    "long something takes, and it holds your turn for the whole guess: messages to you wait until "
    "it ends, and so do you when the thing you are waiting on finishes early. Wait on the event "
    "instead:\n"
    "- Waiting on a command (tests, a build, a script): run that command in the background and "
    "end your turn; its finishing wakes you. Use `python3 "
    'system/scripts/workspace_bare_scripts/run_in_background.py --description "<what>" -- '
    "<command>`, or your harness's own background option if it wakes you when the command "
    "finishes. Do not poll its output.\n"
    "- Waiting on some other state (a service coming back after a restart, a file appearing): "
    "check the state itself with short sleeps, e.g. `until curl -sf <url>; do sleep 1; done`.\n"
    "- A wait that really is about time (the user asked you to check back in N minutes, a service "
    "said to retry after N seconds, pacing calls to a rate-limited API): put the sleep inside the "
    "command you run in the background, and end your turn.\n"
)


def is_blocked(command: str) -> bool:
    """True when `command` runs a `sleep` of 5 seconds or more as one of its own commands."""
    parsed = parse_command(command)
    if parsed is None:
        return False
    for segment in parsed.segments:
        words = list(segment.words)
        while words and (_ENV_ASSIGN.match(words[0]) or words[0] in _LEADING_KEYWORDS):
            words.pop(0)
        if words and os.path.basename(words[0]) == "sleep":
            if _seconds(words[1:]) >= _MIN_BLOCKED_SECONDS:
                return True
    return False


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

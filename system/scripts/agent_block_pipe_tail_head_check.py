#!/usr/bin/env python3
"""Decide whether a Bash command pipes output it cannot get back into `head` or `tail`.

Takes the command as its only argument (passed by agent_block_pipe_tail_head.sh, which calls
this only when the raw text contains a pipe into `head` or `tail`). Exits 0 to allow; exits 2
with the refusal on stderr to BLOCK.

A pipe into `head`/`tail` is allowed when every stage feeding it is a plain read (`cat f | head`,
`git log | grep x | head`), or when a `tee FILE` upstream keeps the full output
(`pytest | tee /tmp/out | tail`). The command is split with the shared `tk_command_parsing`
parser, so each pipeline in `cd x && cat f | head` is judged on its own.

Anything it does not recognise as a plain read is blocked, as is any pipe into `head`/`tail` it
cannot see as a pipeline stage (one inside quoted text, a substitution, or syntax the parser
does not follow).

Runs under a bare `python3` with no virtualenv, like the sibling checkers, so it puts the parser
lib's source directory on `sys.path` itself.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "libs" / "tk_command_parsing" / "src")
)

from tk_command_parsing.parser import CommandSegment, parse_command

# The wrapper's trigger: a pipe (`|` or `|&`, not `||`) into head or tail.
_PIPE_INTO_TRUNCATOR = re.compile(r"(?<!\|)\|&?\s*(tail|head)(\s|$)")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")
_LEADING_KEYWORDS = frozenset(
    {"do", "then", "else", "if", "while", "until", "time", "!"}
)
_TRUNCATORS = frozenset({"head", "tail"})

_READERS = frozenset(
    """
    cat tac nl head tail grep egrep fgrep rg sed awk cut sort uniq tr wc column jq
    ls tree find stat file diff echo printf
    """.split()
)
_GIT_READ_SUBCOMMANDS = frozenset(
    """
    log show diff status grep ls-files ls-tree rev-list rev-parse cat-file blame shortlog
    describe merge-base
    """.split()
)
_GIT_VALUE_OPTIONS = frozenset({"-C", "-c"})

_REFUSAL = (
    "Do not pipe commands through tail or head. Instead, redirect output to a temp file "
    "(e.g. cmd > /tmp/output.txt) and then read from that file separately using the Read tool "
    "or a separate tail/head command on the file.\n"
    "Piping a plain read of files or git history into head/tail is fine (cat, grep, rg, sed, "
    "ls, find, git log/show/diff/status, ...), and so is `cmd | tee /tmp/output.txt | tail`, "
    "which keeps the full output.\n"
)


def is_blocked(command: str) -> bool:
    """True when `command` pipes into head/tail output that would have to be regenerated."""
    parsed = parse_command(command)
    if parsed is None:
        return True
    truncations = 0
    for pipeline in _pipelines(parsed.segments):
        for index, stage in enumerate(pipeline):
            if index > 0 and _command_name(stage) in _TRUNCATORS:
                truncations += 1
                if not _is_recoverable(pipeline[:index]):
                    return True
    # Fewer stages than textual pipes means one sits somewhere the parser does not follow.
    return truncations < len(_PIPE_INTO_TRUNCATOR.findall(command))


def _continues_pipeline(terminator: str | None) -> bool:
    # The lexer returns a run of operators as one token, so `)|` and `|;` (a pipe then a
    # newline) still pipe into the next stage.
    return terminator is not None and "|" in terminator and "||" not in terminator


def _pipelines(segments: tuple[CommandSegment, ...]) -> list[list[CommandSegment]]:
    pipelines: list[list[CommandSegment]] = []
    current: list[CommandSegment] = []
    for segment in segments:
        # A blank line after a pipe reaches here as an empty `;` segment; bash keeps piping.
        if current and not segment.words and set(segment.terminator or "") == {";"}:
            continue
        current.append(segment)
        if not _continues_pipeline(segment.terminator):
            pipelines.append(current)
            current = []
    if current:
        pipelines.append(current)
    return pipelines


def _is_recoverable(producers: list[CommandSegment]) -> bool:
    """True when every stage after the last `tee FILE` is a plain read."""
    for start in range(len(producers) - 1, -1, -1):
        words = _command_words(producers[start])
        if words[:1] == ["tee"] and any(
            not w.startswith(("-", "/dev/")) for w in words[1:]
        ):
            producers = producers[start + 1 :]
            break
    return all(_is_plain_read(stage) for stage in producers)


def _is_plain_read(stage: CommandSegment) -> bool:
    if any("$(" in w or "`" in w for w in stage.words):
        return False
    words = _command_words(stage)
    if not words:
        return False
    name = os.path.basename(words[0])
    if name == "git":
        return _git_subcommand(words[1:]) in _GIT_READ_SUBCOMMANDS
    return name in _READERS or "--help" in words or "--version" in words


def _git_subcommand(args: list[str]) -> str | None:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _GIT_VALUE_OPTIONS else 1
    return args[i] if i < len(args) else None


def _command_words(stage: CommandSegment) -> list[str]:
    words = list(stage.words)
    while words and (_ENV_ASSIGN.match(words[0]) or words[0] in _LEADING_KEYWORDS):
        words.pop(0)
    return words


def _command_name(stage: CommandSegment) -> str | None:
    words = _command_words(stage)
    return os.path.basename(words[0]) if words else None


def main(argv: list[str] | None = None) -> int:
    args = (sys.argv if argv is None else argv)[1:]
    if not is_blocked(args[0] if args else ""):
        return 0
    sys.stderr.write(_REFUSAL)
    return 2


if __name__ == "__main__":
    sys.exit(main())

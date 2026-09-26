#!/usr/bin/env python3
"""Decide whether a Bash command pipes output it cannot get back into `head` or `tail`.

Takes the command as its only argument (passed by agent_block_pipe_tail_head.sh, which calls
this only when the raw text contains a pipe into `head` or `tail`). Exits 0 to allow; exits 2
with the refusal on stderr to BLOCK.

A pipe into `head`/`tail` is allowed when every stage feeding it only reads files, directories,
or git history (`cat f | head`, `git log | grep x | head`, `ls -R | head`), or when a
`tee FILE` upstream already keeps the full output (`pytest | tee /tmp/out | tail`): either way
the rest of the output is still there to read. Anything else -- a test run, a build, a network
call, an unknown program, a command substitution or process substitution feeding the pipe --
is blocked, and so is a command the lexer cannot parse. The command structure comes from the
shared shlex-based `tk_command_parsing` parser, so a `cd x && cat f | head` is judged pipeline
by pipeline rather than as one string. Quoted text that itself contains a pipe into
`head`/`tail` (`bash -c '...'`, `ssh host '...'`) is judged as a command of its own, unless a
plain read receives it as a pattern or text (`grep -E 'error|tail' log`).

Runs under a bare `python3` with no virtualenv, like the sibling checkers, so it puts the parser
lib's source directory on `sys.path` itself; the lib is stdlib-only for the same reason.
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

# The same trigger the wrapper uses: a pipe (`|` or `|&`, not `||`) into head or tail, which
# may be followed by the quote or paren that closes the text it sits in.
_PIPE_INTO_TRUNCATOR = re.compile(r"(?<!\|)\|&?[\s\\]*(tail|head)(?![\w.-])")
# A line continuation, which the shell drops before parsing and shlex would keep as a `\n`
# word. A backslash that is itself escaped (`\\` then a newline) does not continue the line.
_LINE_CONTINUATION = re.compile(r"(?<!\\)((?:\\\\)*)\\\n")
# Process substitution starts a word; `<tag>(` inside quoted text or a heredoc body does not.
_PROCESS_SUBSTITUTION = re.compile(r"(?:^|[\s;&|])[<>]\(")
# What a redirect leaves among a segment's words: the fd number of `2>&1`/`2>` and the target.
_REDIRECT_RESIDUE = re.compile(r"^\d+$|^/dev/")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")
_CONTROL_OPERATOR = re.compile(r"\|\||\|&|&&|[|&;()]")

# Reserved words that can open a stage ahead of the program it runs (`do cat f | head`).
_LEADING_KEYWORDS = frozenset(
    {"do", "then", "else", "elif", "if", "while", "until", "{", "!", "time"}
)

_TRUNCATORS = frozenset({"head", "tail"})
_PIPES = frozenset({"|", "|&"})

# Programs whose output depends only on files, directories, or process state already on the
# machine, so running them again costs about as much as reading a saved copy.
_READERS = frozenset(
    """
    cat tac nl head tail grep egrep fgrep rg sed awk cut sort uniq tr wc column paste fold rev
    comm diff cmp jq xxd od strings sha256sum md5sum
    ls tree find du stat file readlink realpath basename dirname
    echo printf pwd printenv which whoami id date uname hostname ps dmesg free uptime df
    """.split()
)
# Readers that walk a tree: cheap on a project directory, slow on the whole filesystem.
_TREE_WALKERS = frozenset({"find", "du", "rg", "tree"})
# Readers that walk a tree only when given one of these short-flag letters or long flags.
_RECURSIVE_FLAGS = {
    "grep": ("rR", ("--recursive", "--dereference-recursive")),
    "egrep": ("rR", ("--recursive", "--dereference-recursive")),
    "fgrep": ("rR", ("--recursive", "--dereference-recursive")),
    "ls": ("R", ("--recursive",)),
}
# find actions that run a program per match or change the tree, so the walk is no longer a read.
_FIND_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete"})

_GIT_READ_SUBCOMMANDS = frozenset(
    """
    log show diff status grep ls-files ls-tree rev-list rev-parse cat-file blame shortlog
    describe merge-base for-each-ref name-rev show-ref count-objects
    """.split()
)
# git subcommands that only list when given these first arguments (or none, where listed).
_GIT_LISTING_ARGS = {
    "worktree": frozenset({"list"}),
    "stash": frozenset({"list", "show"}),
    "remote": frozenset({"", "-v", "--verbose", "get-url"}),
}
# `git reflog` shows the log unless its first argument is one of these subcommands.
_GIT_REFLOG_WRITES = frozenset({"expire", "delete", "drop"})
# git global options that take a separate value.
_GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
# branch/tag options that put them in list mode (git documents the filters as implying --list).
_GIT_LIST_FLAGS = frozenset(
    {
        "-l",
        "--list",
        "--contains",
        "--no-contains",
        "--merged",
        "--no-merged",
        "--points-at",
    }
)
_GIT_BRANCH_MUTATING_FLAGS = (
    "--set-upstream-to",
    "-u",
    "--unset-upstream",
    "--edit-description",
)

_MAX_QUOTED_DEPTH = 3

_REFUSAL = (
    "Do not pipe commands through tail or head. Instead, redirect output to a temp file "
    "(e.g. cmd > /tmp/output.txt) and then read from that file separately using the Read tool "
    "or a separate tail/head command on the file.\n"
    "Piping a plain read of files or git history into head/tail is fine (cat, grep, rg, sed, "
    "ls, find, git log/show/diff/status, ...), and so is `cmd | tee /tmp/output.txt | tail`, "
    "which keeps the full output.\n"
)


def is_blocked(command: str, depth: int = 0) -> bool:
    """True when `command` pipes into head/tail output that would have to be regenerated."""
    command = _LINE_CONTINUATION.sub(r"\1", command)
    parsed = parse_command(command)
    if parsed is None:
        return True
    is_strict = _PROCESS_SUBSTITUTION.search(command) is not None
    for pipeline in _pipelines(parsed.segments):
        for index, stage in enumerate(pipeline):
            if index > 0 and _command_name(stage) in _TRUNCATORS:
                if is_strict or not _is_recoverable(pipeline[:index]):
                    return True
    if depth < _MAX_QUOTED_DEPTH:
        for segment in parsed.segments:
            # A plain read's quoted words are patterns or text (`grep -E 'a|tail'`), not shell.
            if _is_plain_read(segment):
                continue
            for word in segment.words:
                if _PIPE_INTO_TRUNCATOR.search(word):
                    if is_blocked(word, depth + 1):
                        return True
    return False


def _pipelines(segments: tuple[CommandSegment, ...]) -> list[list[CommandSegment]]:
    """Group segments into pipelines, the stages joined by `|` or `|&`.

    The lexer returns a run of operators as one terminator (`)|`, `|;`), so each is split
    into its operators first: the segment's words end at the first, and every later one ends
    an empty stage, which is how `(pytest)|head` feeds head from a group with no words. The
    parser also turns an unquoted newline into `;`, so a line break after a pipe shows up as
    an empty `;` stage; bash continues the pipeline across it, and so does this.
    """
    pipelines: list[list[CommandSegment]] = []
    current: list[CommandSegment] = []
    is_continuing = False
    for stage in _stages(segments):
        if is_continuing and not stage.words and stage.terminator == ";":
            continue
        current.append(stage)
        is_continuing = stage.terminator in _PIPES
        if not is_continuing:
            pipelines.append(current)
            current = []
    if current:
        pipelines.append(current)
    return pipelines


def _stages(segments: tuple[CommandSegment, ...]) -> list[CommandSegment]:
    stages: list[CommandSegment] = []
    for segment in segments:
        operators = _CONTROL_OPERATOR.findall(segment.terminator or "") or [None]
        stages.append(segment._replace(terminator=operators[0]))
        stages.extend(
            CommandSegment((), False, None, (), operator) for operator in operators[1:]
        )
    return stages


def _is_recoverable(producers: list[CommandSegment]) -> bool:
    """True when the output reaching the truncator can be read again without rerunning
    anything costly: every stage after the last `tee FILE` is a plain read."""
    for start in range(len(producers) - 1, -1, -1):
        if _tees_to_file(producers[start]):
            producers = producers[start + 1 :]
            break
    return all(_is_plain_read(stage) for stage in producers)


def _tees_to_file(stage: CommandSegment) -> bool:
    words = _command_words(stage)
    return (
        bool(words)
        and words[0] == "tee"
        and any(not w.startswith("-") and not w.startswith("/dev/") for w in words[1:])
    )


def _is_plain_read(stage: CommandSegment) -> bool:
    words = _command_words(stage)
    if not words or any("$(" in w or "`" in w for w in stage.words):
        return False
    name = os.path.basename(words[0])
    args = words[1:]
    if name in _READERS:
        # The lexer does not expand globs, so `/*` reaches here as written.
        walks_root = _walks_tree(name, args) and any(
            a.startswith("/") and not a.strip("/*") for a in args
        )
        return not walks_root and (name != "find" or _FIND_ACTIONS.isdisjoint(args))
    if name == "git":
        return _is_git_read(args)
    if name == "supervisorctl":
        return args[:1] == ["status"]
    if name == "crontab":
        return args == ["-l"]
    return "--help" in args or "--version" in args


def _walks_tree(name: str, args: list[str]) -> bool:
    if name in _TREE_WALKERS:
        return True
    if name not in _RECURSIVE_FLAGS:
        return False
    letters, long_flags = _RECURSIVE_FLAGS[name]
    return any(
        a in long_flags
        or (
            a.startswith("-")
            and not a.startswith("--")
            and any(c in a for c in letters)
        )
        for a in args
    )


def _is_git_read(args: list[str]) -> bool:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _GIT_VALUE_OPTIONS else 1
    if i >= len(args):
        return False
    subcommand, rest = args[i], args[i + 1 :]
    if subcommand in _GIT_READ_SUBCOMMANDS:
        return True
    if subcommand in _GIT_LISTING_ARGS:
        return (rest[0] if rest else "") in _GIT_LISTING_ARGS[subcommand]
    if subcommand == "reflog":
        return (rest[0] if rest else "") not in _GIT_REFLOG_WRITES
    if subcommand in ("branch", "tag"):
        # With no name given, branch and tag only list; `-l`/`--list`, or a filter such as
        # `--merged main`, makes a name a pattern or the filter's commit.
        is_listing = all(a.startswith("-") for a in rest) or any(
            a in _GIT_LIST_FLAGS for a in rest
        )
        return is_listing and not any(
            a.startswith(_GIT_BRANCH_MUTATING_FLAGS) for a in rest
        )
    return False


def _command_words(stage: CommandSegment) -> list[str]:
    words = list(stage.words)
    while words and (_ENV_ASSIGN.match(words[0]) or words[0] in _LEADING_KEYWORDS):
        words.pop(0)
    if stage.has_redirect:
        words = [w for w in words if not _REDIRECT_RESIDUE.match(w)]
    return words


def _command_name(stage: CommandSegment) -> str | None:
    words = _command_words(stage)
    # The lexer leaves a closing backtick on the word before it (`pytest | tail`).
    return os.path.basename(words[0]).rstrip("`") if words else None


def main(argv: list[str] | None = None) -> int:
    args = (sys.argv if argv is None else argv)[1:]
    if not is_blocked(args[0] if args else ""):
        return 0
    sys.stderr.write(_REFUSAL)
    return 2


if __name__ == "__main__":
    sys.exit(main())

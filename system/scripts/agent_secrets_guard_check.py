#!/usr/bin/env python3
"""Decide whether a tool call reads or writes a secret file directly.

Takes the hook payload (``{"tool_name": ..., "tool_input": {...}}``) on stdin,
as agent_secrets_guard.sh hands it over. Exits 0 to allow; exits 2 with a
guiding stderr message to BLOCK. See the wrapper for the why.

Two halves, one rule (policy P8): a value stored under ``data/.secrets/`` may
reach a process only through ``with_secrets.py``.

* A **shell** command (claude's and codex's ``Bash``, pi's ``bash``, the
  ``Bash`` payload the agy shim synthesises) is tokenized with the shared
  ``tk_command_parsing`` parser, so a rationale that happens to mention the
  directory inside a quoted argument stays inside one token. Any segment whose
  words mention ``data/.secrets`` passes only when its program is the wrapper or
  the request script (directly, or through ``python3`` / ``uv run``), or is
  ``ls`` or ``rm``. A ``bash -c "..."`` string is unwrapped and judged by the
  same rule, which is how a supervisord program command passes.
* A **file** tool (claude's Read / Grep / Glob / Edit / Write, pi's read / edit /
  write / grep / find, codex's ``apply_patch``) is refused when its path, or the
  patch's file lines, point under ``data/.secrets/``. The directory's README is
  not a secret and stays readable.

Nothing here prints the command or a path back: either may carry a value.

This hook runs under a bare ``python3`` with no virtualenv (see the wrapper), so
it puts the parser lib's source directory on ``sys.path`` explicitly rather than
relying on an installed package; the lib is stdlib-only for the same reason.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "libs" / "tk_command_parsing" / "src")
)

from tk_command_parsing.parser import parse_command

SECRETS_DIRECTORY = "data/.secrets"
# The one file under the directory that holds no secret.
_README_NAME = "README.md"

# The programs a shell segment may run while naming the directory. The wrapper is
# how a value reaches a process; the request script names the directory in the
# path it prints; ls and rm are how an agent lists and removes secret files.
WRAPPER_SCRIPT = "with_secrets.py"
REQUEST_SCRIPT = "request_secret.py"
_ALLOWED_SCRIPTS = frozenset({WRAPPER_SCRIPT, REQUEST_SCRIPT})
_ALLOWED_PROGRAMS = frozenset({"ls", "rm"})
_PYTHON_PROGRAMS = frozenset({"python", "python3"})
# Python's own flags that keep the next argument a script rather than code.
_PYTHON_PASSTHROUGH_FLAGS = frozenset(
    {"-u", "-B", "-E", "-s", "-S", "-I", "-O", "-OO", "-q"}
)
_SHELL_PROGRAMS = frozenset({"bash", "sh", "zsh", "dash"})
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_]\w*=")

# Tool names whose payload is a shell command: claude and codex ``Bash``, pi's
# ``bash``, and the empty name the agy shim's synthesised payload may carry.
_SHELL_TOOL_NAMES = frozenset({"Bash", "bash", ""})
# claude's file tools plus pi's; codex edits files through apply_patch.
_PATH_TOOL_NAMES = frozenset(
    {
        "Read",
        "Grep",
        "Glob",
        "Edit",
        "Write",
        "MultiEdit",
        "NotebookEdit",
        "NotebookRead",
        "read",
        "edit",
        "write",
        "grep",
        "find",
    }
)
_PATCH_TOOL_NAME = "apply_patch"
# Where a file tool names its target. ``pattern`` is a path only for the glob
# tools (claude's Glob, pi's find); Grep's ``pattern`` is a regex and is left alone.
_PATH_FIELDS = ("file_path", "path", "notebook_path")
_GLOB_TOOL_NAMES = frozenset({"Glob", "find"})
_PATCH_FILE_LINE_RE = re.compile(
    r"^\*\*\* (?:Add|Update|Delete) File: (.*)$", re.MULTILINE
)

_SHELL_REASON = (
    "it reads, writes, or otherwise touches a file under data/.secrets/ directly"
)
_UNPARSEABLE_REASON = (
    "it mentions data/.secrets/ and could not be parsed as a shell command"
)
_FILE_TOOL_REASON = "it opens a file under data/.secrets/"


def _is_secret_path_mention(word: str) -> bool:
    """Whether ``word`` names something under the secrets directory other than its README."""
    if SECRETS_DIRECTORY not in word:
        return False
    return not word.rstrip("/").endswith(f"{SECRETS_DIRECTORY}/{_README_NAME}")


def _basename(word: str) -> str:
    return PurePosixPath(word).name


def _strip_leading_assignments(words: Sequence[str]) -> tuple[str, ...]:
    """The words after any ``VAR=value`` prefixes (and an ``env`` in front of them)."""
    remaining = list(words)
    while remaining and (
        _ENV_ASSIGNMENT_RE.match(remaining[0]) or remaining[0] == "env"
    ):
        remaining.pop(0)
    return tuple(remaining)


def _runs_allowed_script(words: Sequence[str]) -> bool:
    """Whether ``words`` invoke the wrapper or the request script as their program.

    Accepts the script run directly, through ``python3`` (with python's own
    passthrough flags, never ``-c`` or ``-m``), or through ``uv run [python3]``.
    """
    remaining = list(words)
    if remaining[:2] == ["uv", "run"]:
        remaining = remaining[2:]
    if remaining and _basename(remaining[0]) in _PYTHON_PROGRAMS:
        remaining = remaining[1:]
        while remaining and remaining[0] in _PYTHON_PASSTHROUGH_FLAGS:
            remaining = remaining[1:]
    return bool(remaining) and _basename(remaining[0]) in _ALLOWED_SCRIPTS


def _segment_violation(words: Sequence[str]) -> str | None:
    """The reason one command segment is refused, or None when it is allowed."""
    # A nested shell string is a command of its own: judge it by the same rule and
    # take it out of this segment's words, so a wrapper command inside `bash -c`
    # passes while the outer program (an OOM tag, supervisord's shell) is not asked
    # to be on the allow list.
    outer: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        is_shell_string = (
            _basename(word) in _SHELL_PROGRAMS
            and index + 2 < len(words)
            and words[index + 1] == "-c"
        )
        if is_shell_string:
            inner_violation = classify_command(words[index + 2])
            if inner_violation is not None:
                return inner_violation
            outer.extend([word, words[index + 1]])
            index += 3
            continue
        outer.append(word)
        index += 1

    if not any(_is_secret_path_mention(word) for word in outer):
        return None
    program_words = _strip_leading_assignments(outer)
    if not program_words:
        return _SHELL_REASON
    if _basename(program_words[0]) in _ALLOWED_PROGRAMS:
        return None
    if _runs_allowed_script(program_words):
        return None
    return _SHELL_REASON


def classify_command(command: str) -> str | None:
    """The reason a shell command is refused, or None when it is allowed."""
    if SECRETS_DIRECTORY not in command:
        return None
    parsed = parse_command(command)
    if parsed is None:
        return _UNPARSEABLE_REASON
    for segment in parsed.segments:
        violation = _segment_violation(segment.words)
        if violation is not None:
            return violation
    return None


def _file_tool_targets(tool_name: str, tool_input: Mapping[str, object]) -> list[str]:
    """Every path a file tool names in its input."""
    fields = list(_PATH_FIELDS)
    if tool_name in _GLOB_TOOL_NAMES:
        fields.append("pattern")
    return [
        str(tool_input[field])
        for field in fields
        if isinstance(tool_input.get(field), str)
    ]


def classify_file_tool(tool_name: str, tool_input: Mapping[str, object]) -> str | None:
    targets = _file_tool_targets(tool_name, tool_input)
    return (
        _FILE_TOOL_REASON
        if any(_is_secret_path_mention(target) for target in targets)
        else None
    )


def classify_patch(patch_body: str) -> str | None:
    file_lines = _PATCH_FILE_LINE_RE.findall(patch_body)
    return (
        _FILE_TOOL_REASON
        if any(_is_secret_path_mention(line.strip()) for line in file_lines)
        else None
    )


def classify_payload(payload: Mapping[str, object]) -> str | None:
    """The reason the tool call in ``payload`` is refused, or None when it is allowed."""
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        return None
    if tool_name in _SHELL_TOOL_NAMES:
        command = tool_input.get("command")
        return classify_command(command) if isinstance(command, str) else None
    if tool_name == _PATCH_TOOL_NAME:
        patch_body = tool_input.get("command")
        return classify_patch(patch_body) if isinstance(patch_body, str) else None
    if tool_name in _PATH_TOOL_NAMES:
        return classify_file_tool(tool_name, tool_input)
    return None


def _block_message(reason: str) -> str:
    return (
        "Blocked: a secret file is read only by with_secrets.py -- " + reason + ".\n\n"
        "Files under data/.secrets/ hold values the user handed the chat app through a "
        "secret card so they would never enter this transcript. Reading, printing, "
        "copying, or editing one puts the value into a tool call, which defeats that. "
        "Run the program that needs the value under the wrapper instead; it puts the "
        "file's variables into that process's environment and nothing else:\n"
        "  python3 system/scripts/with_secrets.py data/.secrets/<name>.env -- <command...>\n\n"
        "Listing the directory (`ls`) and deleting a file (`rm`) are allowed. To change "
        "a value, request it again with request_secret.py rather than editing the file. "
        "See the connect-external-service skill.\n"
    )


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    violation = classify_payload(payload)
    if violation is None:
        return 0
    sys.stderr.write(_block_message(violation))
    return 2


if __name__ == "__main__":
    sys.exit(main())

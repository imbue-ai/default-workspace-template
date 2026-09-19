#!/usr/bin/env python3
"""Run a command with the variables of one ``data/.secrets/<name>.env`` file in its environment.

Usage, from the repo root::

    python3 system/scripts/with_secrets.py data/.secrets/<name>.env -- <command...>

This is the one sanctioned way a stored secret reaches a process: an agent's own
command, an ``.mcp.json`` server command, a supervisord program, or a scheduled
job names the env file here instead of reading it, so the value never appears in
a tool call, a config file, or a transcript. The PreToolUse guard
(``agent_secrets_guard.sh``, policy P8 in
``system/apps/chat/imbue/chat/harnesses/core-contracts/tool-call-policies.md``)
allows a shell command to mention ``data/.secrets`` only through this script.

The file is refused unless it sits under a ``data/.secrets/`` directory and is
readable by its owner alone: a group- or world-readable secret file is a mistake
worth stopping on rather than working around. Each line is ``NAME='value'`` with
POSIX single-quote escaping (``'\\''`` for a literal quote), the form the chat app
writes; a hand-written file may also use double quotes or no quotes. Blank lines,
``#`` comments, and a leading ``export `` are accepted.

Standard library only: supervisord programs and cron jobs run it before any venv
exists, and it must never import anything that could log its environment.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

SECRETS_DIRECTORY_PARTS = ("data", ".secrets")
ENV_FILE_SUFFIX = ".env"
ARGUMENT_SEPARATOR = "--"

# A POSIX shell identifier, which is what `source` would accept as a name.
_VARIABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EXPORT_PREFIX = "export "

# Permission bits nobody but the owner may hold on a secret file.
_GROUP_AND_OTHER_BITS = stat.S_IRWXG | stat.S_IRWXO

EXIT_USAGE = 2


class WithSecretsError(Exception):
    """Raised when the env file cannot be used or the invocation is malformed."""


def is_under_secrets_directory(env_file: Path) -> bool:
    """Whether ``env_file`` is a ``.env`` file directly inside some ``data/.secrets/``."""
    resolved = env_file.resolve()
    return (
        resolved.suffix == ENV_FILE_SUFFIX
        and resolved.parent.name == SECRETS_DIRECTORY_PARTS[1]
        and resolved.parent.parent.name == SECRETS_DIRECTORY_PARTS[0]
    )


def check_env_file_permissions(env_file: Path) -> None:
    """Raise WithSecretsError unless the file exists and only its owner can read it."""
    try:
        mode = env_file.stat().st_mode
    except FileNotFoundError:
        raise WithSecretsError(f"{env_file} does not exist") from None
    except OSError as exc:
        raise WithSecretsError(f"{env_file} cannot be read: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise WithSecretsError(f"{env_file} is not a regular file")
    if mode & _GROUP_AND_OTHER_BITS:
        raise WithSecretsError(
            f"{env_file} is readable by more than its owner (mode {stat.S_IMODE(mode):04o}); "
            "run `chmod 600` on it first"
        )


class _EnvFileScanner:
    """A cursor over the file text, so a quoted value may span lines."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.position = 0

    def is_done(self) -> bool:
        return self.position >= len(self.text)

    def peek(self) -> str:
        return self.text[self.position] if not self.is_done() else ""

    def line_number(self) -> int:
        return self.text.count("\n", 0, self.position) + 1

    def skip_blank_and_comment_lines(self) -> None:
        while not self.is_done():
            line_end = self.text.find("\n", self.position)
            line_end = len(self.text) if line_end < 0 else line_end
            if self.text[self.position : line_end].strip() and not self.text[self.position : line_end].lstrip().startswith("#"):
                self.position += len(self.text[self.position:line_end]) - len(self.text[self.position:line_end].lstrip())
                return
            self.position = line_end + 1

    def read_name(self) -> str:
        equals = self.text.find("=", self.position)
        line_end = self.text.find("\n", self.position)
        if equals < 0 or (0 <= line_end < equals):
            raise WithSecretsError("a line is not a NAME=value assignment")
        name = self.text[self.position : equals]
        if name.startswith(_EXPORT_PREFIX):
            name = name[len(_EXPORT_PREFIX) :].lstrip()
        if not _VARIABLE_NAME_RE.match(name):
            raise WithSecretsError("a line is not a NAME=value assignment")
        self.position = equals + 1
        return name

    def read_single_quoted(self) -> str:
        """`'a'\\''b'` -> `a'b`, however many lines the quoted runs span."""
        pieces: list[str] = []
        while self.peek() == "'":
            end = self.text.find("'", self.position + 1)
            if end < 0:
                raise WithSecretsError("a single-quoted value is missing its closing quote")
            pieces.append(self.text[self.position + 1 : end])
            self.position = end + 1
            if self.text.startswith("\\'", self.position):
                pieces.append("'")
                self.position += 2
        self._end_of_value()
        return "".join(pieces)

    def read_double_quoted(self) -> str:
        """Only `\\"` and `\\\\` are escapes; anything else is literal."""
        pieces: list[str] = []
        self.position += 1
        while True:
            if self.is_done():
                raise WithSecretsError("a double-quoted value is missing its closing quote")
            character = self.peek()
            if character == "\\" and self.text[self.position + 1 : self.position + 2] in ('"', "\\"):
                pieces.append(self.text[self.position + 1])
                self.position += 2
                continue
            self.position += 1
            if character == '"':
                break
            pieces.append(character)
        self._end_of_value()
        return "".join(pieces)

    def read_unquoted(self) -> str:
        line_end = self.text.find("\n", self.position)
        line_end = len(self.text) if line_end < 0 else line_end
        value = self.text[self.position : line_end].strip()
        self.position = line_end + 1
        return value

    def _end_of_value(self) -> None:
        """A quoted value must be followed by the end of its line."""
        line_end = self.text.find("\n", self.position)
        line_end = len(self.text) if line_end < 0 else line_end
        if self.text[self.position : line_end].strip():
            raise WithSecretsError("a quoted value has text after its closing quote")
        self.position = line_end + 1


def parse_env_file(text: str) -> dict[str, str]:
    """Every variable the file sets, later lines winning over earlier ones.

    Each entry is ``NAME=value``: single-quoted with POSIX escaping (the form the chat
    app writes; the value may span lines), double-quoted, or bare to the end of the
    line. Blank lines, ``#`` comments, and a leading ``export `` are accepted. A line
    that is none of these raises WithSecretsError: the file is written by a program,
    so a malformed line means something other than the chat app edited it, and
    running with half the variables would be worse than stopping.
    """
    scanner = _EnvFileScanner(text)
    value_by_name: dict[str, str] = {}
    while True:
        scanner.skip_blank_and_comment_lines()
        if scanner.is_done():
            return value_by_name
        line_number = scanner.line_number()
        try:
            name = scanner.read_name()
            if scanner.peek() == "'":
                value = scanner.read_single_quoted()
            elif scanner.peek() == '"':
                value = scanner.read_double_quoted()
            else:
                value = scanner.read_unquoted()
        except WithSecretsError as exc:
            raise WithSecretsError(f"line {line_number}: {exc}") from None
        value_by_name[name] = value


def load_env_file(env_file: Path) -> dict[str, str]:
    """The variables of one secret file, after the location and permission checks."""
    if not is_under_secrets_directory(env_file):
        raise WithSecretsError(
            f"{env_file} is not a data/.secrets/<name>.env file; only files there may be loaded"
        )
    check_env_file_permissions(env_file)
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise WithSecretsError(f"{env_file} cannot be read: {exc}") from exc
    try:
        return parse_env_file(text)
    except WithSecretsError as exc:
        raise WithSecretsError(f"{env_file} is malformed: {exc}") from None


def split_arguments(argv: Sequence[str]) -> tuple[Path, tuple[str, ...]]:
    """The env file and the command from ``<env-file> -- <command...>``."""
    if len(argv) < 3 or argv[1] != ARGUMENT_SEPARATOR:
        raise WithSecretsError(
            "usage: with_secrets.py data/.secrets/<name>.env -- <command> [args...]"
        )
    return Path(argv[0]), tuple(argv[2:])


def child_environment(
    parent: Mapping[str, str], value_by_name: Mapping[str, str]
) -> dict[str, str]:
    return {**parent, **value_by_name}


def main(argv: Sequence[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    try:
        env_file, command = split_arguments(arguments)
        value_by_name = load_env_file(env_file)
    except WithSecretsError as exc:
        print(f"with_secrets: {exc}", file=sys.stderr)
        return EXIT_USAGE
    environment = child_environment(os.environ, value_by_name)
    try:
        os.execvpe(command[0], list(command), environment)
    except OSError as exc:
        print(f"with_secrets: cannot run {command[0]}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return 0


if __name__ == "__main__":
    sys.exit(main())

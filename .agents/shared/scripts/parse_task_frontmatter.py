#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Parse a worker task file's YAML frontmatter and emit its string fields.

Pins the required schema so workers can't silently consume a task file
whose `finish_report_path` was missing, misspelled, or the wrong type.
`lead_agent` is the report's push address and is normally stamped by
`create_worker.py launch`; it is deliberately OPTIONAL here (absent ->
warn on stderr, emit no `LEAD_AGENT` line) because a task file can be
authored by a *newer* flow than the launcher that provisioned the worker
(update-self stages the target version's prose for an older lead), and a
worker that finished its task must never be structurally unable to say
so -- with no address, the worker falls back to the same-repo delivery
in `worker-reporting.md`. `task_file` -- the task file's own path,
stamped by the same launch -- is optional on the same terms and for the
same reason. Beyond those, any additional top-level string fields the
lead sets are passed through to the worker -- so leads can attach
flow-specific context (a ticket id, a feature flag, a list of staged
inputs) without each new key requiring a parser change.

The positional argument is an exact path to one task file -- no globs,
no searching. A worker is handed its task file's exact path in the
message that launched it (``launch`` stamps ``task_file`` into the
frontmatter for exactly this), so there is nothing to resolve: a
dispatch nested two levels deep, or two sibling workers under one lead,
can all use the same directory names and still name distinct files.

On success (exit 0) prints shell-evalable ``KEY=value`` lines to
stdout (values quoted via ``shlex.quote`` so whitespace and shell
metacharacters survive). The well-known fields come first in fixed
order; any extra string fields follow alphabetically:

    LEAD_AGENT=crystallize-test
    TASK_FILE=data/.tasks/harden/update-foo/task.md
    FINISH_REPORT_PATH=data/.tasks/harden/update-foo/reports/report.md
    TICKET_ID=task-42

Non-string frontmatter values (lists, mappings, numbers, bools) are
silently dropped -- only strings round-trip cleanly through ``eval``.
Extra string keys must be valid POSIX shell identifiers (so the
``KEY=value`` line a downstream ``eval`` consumes actually creates a
variable instead of being parsed as a command). A key like
``staged-inputs`` fails loud rather than silently disappearing.

On any failure -- file missing, no/broken frontmatter, any required
field missing, wrong type, empty string, or an extra key that isn't a
valid shell identifier -- prints a human-readable error to stderr and
exits 1.
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import yaml

_REQUIRED_FIELDS = ("finish_report_path",)
# Optional fields the launcher stamps: validated like a required field when
# present, but a task file without one still parses (with a stderr warning for
# the address) -- see module docstring.
_ADDRESS_FIELD = "lead_agent"
_TASK_FILE_FIELD = "task_file"
_OPTIONAL_KNOWN_FIELDS = (_ADDRESS_FIELD, _TASK_FILE_FIELD)
# Fixed emission order for the well-known fields (address first when present).
_ORDERED_KNOWN_FIELDS = (_ADDRESS_FIELD, _TASK_FILE_FIELD, *_REQUIRED_FIELDS)
_SHELL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _split_frontmatter(text: str) -> dict[str, Any]:
    """Parse leading ``---`` YAML frontmatter; return the mapping.

    Raises ``ValueError`` if the frontmatter is missing, unterminated,
    not valid YAML, or not a mapping.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("task file must start with `---` frontmatter delimiter")
    try:
        end_idx = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("task file frontmatter is not terminated with `---`") from exc
    fm_text = "\n".join(lines[1:end_idx])
    try:
        parsed = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"task file frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("task file frontmatter must be a YAML mapping")
    return parsed


def parse(task_file: Path) -> dict[str, str]:
    """Return all top-level string fields after validating the well-known ones.

    Required fields (``finish_report_path``) must be present, string-typed,
    and non-empty -- any violation raises ``ValueError``. ``lead_agent`` and
    ``task_file`` are validated the same way when present, but their *absence*
    is not fatal (see module docstring: a task file authored by a newer flow
    than the launcher may legitimately lack them). A missing ``lead_agent``
    additionally warns on stderr, because the worker then has no push address
    and must fall back to same-repo delivery; a missing ``task_file`` costs the
    worker nothing it did not already have, so it passes quietly. Beyond those,
    all other top-level string-valued keys are passed through; non-string values
    are silently dropped. Extra keys must also be valid POSIX shell identifiers
    (``[A-Za-z_][A-Za-z0-9_]*``) so the downstream ``eval`` actually defines
    a variable rather than silently parsing the rendered line as a command
    lookup.
    """
    if not task_file.is_file():
        raise ValueError(f"task file not found: {task_file}")
    frontmatter = _split_frontmatter(task_file.read_text(encoding="utf-8"))
    for field in _REQUIRED_FIELDS + _OPTIONAL_KNOWN_FIELDS:
        if field not in frontmatter:
            if field in _REQUIRED_FIELDS:
                raise ValueError(f"frontmatter is missing required field `{field}`")
            if field == _ADDRESS_FIELD:
                print(
                    f"warning: task frontmatter has no `{_ADDRESS_FIELD}` (the "
                    "launcher predates launch-time stamping?); report pushes "
                    "cannot be addressed -- use the same-repo fallback delivery "
                    "in worker-reporting.md.",
                    file=sys.stderr,
                )
            continue
        value = frontmatter[field]
        if not isinstance(value, str):
            raise ValueError(
                f"frontmatter.{field} must be a string, got {type(value).__name__}"
            )
        if not value:
            raise ValueError(f"frontmatter.{field} must not be empty")
    result = {
        key: value
        for key, value in frontmatter.items()
        if isinstance(value, str) and value
    }
    for key in result:
        if key in _ORDERED_KNOWN_FIELDS:
            continue
        if not _SHELL_IDENTIFIER_RE.match(key):
            raise ValueError(
                f"frontmatter key `{key}` is not a valid shell identifier "
                f"(must match {_SHELL_IDENTIFIER_RE.pattern}); rename it "
                f"using snake_case so downstream `eval` can consume the "
                f"rendered KEY=value line."
            )
    return result


def _render(fields: dict[str, str]) -> str:
    extras = sorted(key for key in fields if key not in _ORDERED_KNOWN_FIELDS)
    ordered = [*_ORDERED_KNOWN_FIELDS, *extras]
    lines = [
        f"{key.upper()}={shlex.quote(fields[key])}" for key in ordered if key in fields
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "task_file",
        type=Path,
        help=(
            "Exact path to the worker task file (markdown with YAML "
            "frontmatter). Not a glob: the worker is handed this path in "
            "the message that launched it."
        ),
    )
    args = parser.parse_args()

    try:
        fields = parse(args.task_file)
    except ValueError as exc:
        print(f"invalid task frontmatter: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(_render(fields))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Print a workspace's template base: the pristine template commit it is on now.

The base comes from the NEWEST template-state marker on HEAD's first-parent
history:

- ``Initial workspace commit`` -- bootstrap writes it on top of the cloned
  template, so the marker itself is the base.
- an ``update-self:`` merge -- the base is its SECOND parent, the upstream
  template commit it merged. Never the merge itself: its tree and its
  first-parent history hold everything the workspace built before the update,
  so treating it as a base publishes (or hides) all of that.

A commit whose subject starts ``update-self:`` but is not a two-parent merge
merged nothing from upstream, so it is not a marker.

``--origin`` prints the workspace's own creation commit instead -- see
:func:`find_workspace_origin`.

Usage (cwd = the repo, or pass ``--repo``):

    uv run .agents/shared/scripts/resolve_template_base.py [--repo DIR] [--origin]

Prints the resolved sha on stdout and exits 0; exits 1 with a message on
stderr when HEAD's first-parent history has no marker.

To apply either rule to a log read some other way (such as over SSH), run
``git`` with :data:`FIRST_PARENT_LOG_ARGS` and pass its lines to
:func:`find_template_base` or :func:`find_workspace_origin`.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Sequence

# Tab-separated so a subject containing spaces parses unambiguously.
FIRST_PARENT_LOG_ARGS = ("log", "--first-parent", "--format=%H%x09%P%x09%s", "HEAD")

_INITIAL_WORKSPACE_COMMIT_SUBJECT = "Initial workspace commit"
_UPDATE_SELF_SUBJECT_PREFIX = "update-self:"


def find_template_base(first_parent_log: Sequence[str]) -> str | None:
    """Return the template base named by the newest marker, or None without one.

    ``first_parent_log`` is the output of ``git`` with
    :data:`FIRST_PARENT_LOG_ARGS`, newest first: ``<sha>\\t<parents>\\t<subject>``.
    """
    for line in first_parent_log:
        if not line.strip():
            continue
        sha, parents, subject = line.split("\t", 2)
        if subject == _INITIAL_WORKSPACE_COMMIT_SUBJECT:
            return sha
        parent_shas = parents.split()
        if subject.startswith(_UPDATE_SELF_SUBJECT_PREFIX) and len(parent_shas) == 2:
            return parent_shas[1]
    return None


def find_workspace_origin(first_parent_log: Sequence[str]) -> str | None:
    """Return the workspace's own creation commit, or None without one.

    Bootstrap writes exactly one ``Initial workspace commit`` per workspace, so
    the NEWEST one on the first-parent chain is this workspace's and everything
    the workspace ever committed descends from it. Older ones belong to someone
    else: the template repo is itself developed from workspaces, and a mind
    created from a published template carries the source mind's marker too.
    Dating a workspace by one of those reports a stranger's creation, and
    treating one as the boundary of the workspace's own work rejects every
    legitimate template base.

    ``first_parent_log`` is as for :func:`find_template_base`.
    """
    for line in first_parent_log:
        if not line.strip():
            continue
        sha, _parents, subject = line.split("\t", 2)
        if subject == _INITIAL_WORKSPACE_COMMIT_SUBJECT:
            return sha
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument(
        "--origin",
        action="store_true",
        help="print the workspace's own creation commit instead of its template base",
    )
    args = parser.parse_args(argv)
    log = subprocess.run(
        ["git", "-C", str(args.repo), *FIRST_PARENT_LOG_ARGS],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    lines = log.splitlines()
    if args.origin:
        resolved = find_workspace_origin(lines)
        missing = "no 'Initial workspace commit' on HEAD's first-parent history"
    else:
        resolved = find_template_base(lines)
        missing = (
            "no 'Initial workspace commit' or 'update-self:' merge on HEAD's "
            "first-parent history"
        )
    if resolved is None:
        print(f"resolve_template_base.py: {missing}", file=sys.stderr)
        return 1
    print(resolved)
    return 0


if __name__ == "__main__":
    sys.exit(main())

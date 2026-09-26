#!/usr/bin/env python3
"""Copy an app's data on disk, after checking the copy fits there.

Two copies of an app's data are made while changing the app, and both come
through here:

- A throwaway instance's writable copy (``serve_isolated_instance.py --copy``),
  under ``data/.state/isolated-instances/<name>/copies/<key>/``. ``down`` removes it.
- The recovery net ``update-app`` takes before changing a live store in place
  (``snapshot`` below), under ``data/.state/app-data-snapshots/<app>/<label>/``.
  ``drop`` removes it once the change is confirmed.

Neither belongs in ``/tmp``. A workspace container's ``/tmp`` is a tmpfs, so a
file there is held in memory and counts against the container's memory limit,
and earlyoom cannot free it by killing a process. An app's data can be larger
than the container's memory: copying one such store there got the whole
workspace killed. Both locations above are on disk and excluded from the
host-backup snapshot, so a multi-GB copy does not ride every backup either.

The disk is finite too, and the live workspace shares it: a copy that would
leave less than ``RESERVE_BYTES`` free is refused before anything is written,
and a copy that fails part way is removed rather than left holding the space.

Run via bare ``python3`` (standard library only), from the repo root:

    python3 .agents/shared/scripts/copy_app_data.py snapshot --app <name> --label <change>
    python3 .agents/shared/scripts/copy_app_data.py drop --app <name> --label <change>

``snapshot`` prints the snapshot's path on stdout.

Exit codes:
    0  Success (snapshot taken / dropped, or nothing to drop).
    1  Refused or failed: the copy would not fit, the app has no data directory,
       a snapshot by that label already exists, or a name is not a plain slug.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import Callable, Sequence

APPS_DATA_ROOT = "data/.apps"
SNAPSHOT_ROOT = "data/.state/app-data-snapshots"

# What a copy must leave free on its disk, for the live workspace's own writes.
RESERVE_BYTES = 2 * 1024**3

_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class CopyError(Exception):
    """A copy was refused, or could not be made."""


def free_bytes(path: Path) -> int:
    """Free space on the filesystem holding ``path``, which must exist."""
    return shutil.disk_usage(path).free


def tree_size_bytes(root: Path) -> int:
    """The bytes a copy of ``root`` writes: every regular file's size, symlinks not followed."""
    total = 0
    for directory, _subdirectories, filenames in os.walk(root):
        for filename in filenames:
            stat_result = os.lstat(os.path.join(directory, filename))
            if stat.S_ISREG(stat_result.st_mode):
                total += stat_result.st_size
    return total


def _format_bytes(count: int) -> str:
    return (
        f"{count / 1024**3:.1f} GB" if count >= 1024**3 else f"{count / 1024**2:.1f} MB"
    )


def copy_tree_checked(
    source: Path,
    destination: Path,
    *,
    free_space: Callable[[Path], int] = free_bytes,
) -> None:
    """Copy the directory ``source`` to ``destination`` (which must not exist yet).

    Refuses, writing nothing, when the copy would leave less than ``RESERVE_BYTES``
    free on the destination's disk; a copy that fails part way is removed.
    """
    if destination.exists():
        raise CopyError(f"{destination} already exists; not copying over it")
    destination.parent.mkdir(parents=True, exist_ok=True)
    size = tree_size_bytes(source)
    available = free_space(destination.parent)
    if size + RESERVE_BYTES > available:
        raise CopyError(
            f"copying {source} ({_format_bytes(size)}) to {destination} would leave "
            f"{_format_bytes(max(available - size, 0))} free on that disk, under the "
            f"{_format_bytes(RESERVE_BYTES)} the workspace needs for its own writes. "
            "Copy only what the test needs (a subdirectory, or a small store seeded "
            "for the test), or verify read-only against the live service. Do not copy "
            "it into /tmp instead: /tmp is memory, and a copy that size takes the "
            "whole workspace down."
        )
    try:
        shutil.copytree(source, destination, symlinks=True)
    except OSError as exc:
        shutil.rmtree(destination, ignore_errors=True)
        raise CopyError(f"copying {source} to {destination} failed: {exc}") from exc


def _require_slug(kind: str, value: str) -> None:
    if not _SLUG_PATTERN.match(value):
        raise CopyError(
            f"--{kind} must be a plain name (letters, digits, '.', '_', '-'), got {value!r}"
        )


def snapshot_path(repo_root: Path, app: str, label: str) -> Path:
    return repo_root / SNAPSHOT_ROOT / app / label


def snapshot(
    repo_root: Path,
    app: str,
    label: str,
    *,
    free_space: Callable[[Path], int] = free_bytes,
) -> Path:
    """Copy ``data/.apps/<app>/`` to its snapshot path for ``label``; return that path."""
    _require_slug("app", app)
    _require_slug("label", label)
    source = repo_root / APPS_DATA_ROOT / app
    if not source.is_dir():
        raise CopyError(f"{source} is not a directory; there is no data to snapshot")
    destination = snapshot_path(repo_root, app, label)
    if destination.exists():
        raise CopyError(
            f"a snapshot already exists at {destination}; drop it first or pick another --label"
        )
    copy_tree_checked(source, destination, free_space=free_space)
    return destination


def drop(repo_root: Path, app: str, label: str) -> bool:
    """Remove the snapshot for ``label``; return whether there was one."""
    _require_slug("app", app)
    _require_slug("label", label)
    destination = snapshot_path(repo_root, app, label)
    if not destination.exists():
        return False
    shutil.rmtree(destination)
    if not any(destination.parent.iterdir()):
        destination.parent.rmdir()
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Snapshot an app's data on disk before changing it in place."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("snapshot", "Copy data/.apps/<app>/ aside; print where it went."),
        ("drop", "Remove a snapshot once the change is confirmed. Idempotent."),
    ):
        subparser = subparsers.add_parser(command, help=help_text)
        subparser.add_argument("--app", required=True, help="The app's name.")
        subparser.add_argument(
            "--label",
            required=True,
            help="What the snapshot is before, e.g. 'pre-schema-v2'.",
        )
        subparser.add_argument(
            "--repo-root",
            default=".",
            help="Path to the repository root (default: current directory).",
        )
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    try:
        if args.command == "snapshot":
            sys.stdout.write(f"{snapshot(repo_root, args.app, args.label)}\n")
            return 0
        if not drop(repo_root, args.app, args.label):
            sys.stderr.write(
                f"no snapshot '{args.label}' for '{args.app}'; nothing to drop.\n"
            )
        return 0
    except (CopyError, OSError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())

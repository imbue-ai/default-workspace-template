#!/usr/bin/env python3
"""Count how many times the welcome skill has run in this workspace, and take the next number.

Usage, from the repo root::

    python3 system/scripts/welcome_count.py        # prints the number of earlier runs, then records this one
    python3 system/scripts/welcome_count.py --peek # prints the number of earlier runs and records nothing

The welcome skill (``.agents/skills/welcome``) varies what it says by how many chats have been
greeted before: the first greeting after the onboarding chat explains that every chat talks to
the same Mind, the next few offer a different hint each, and the rest are a plain greeting. The
count is kept under ``data/.state/`` (machine state the workspace can rebuild, not the user's
data), one small file, so it survives the agent that read it and the restarts in between.

Standard library only: the skill runs this as ``python3 system/scripts/...``.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from pathlib import Path

DEFAULT_COUNT_PATH = Path("data/.state/welcome/count")
ENV_COUNT_PATH = "MINDS_WELCOME_COUNT_FILE"


def count_path(environ: Mapping[str, str], cwd: Path) -> Path:
    path = Path(environ.get(ENV_COUNT_PATH) or DEFAULT_COUNT_PATH)
    return path if path.is_absolute() else cwd / path


def read_count(path: Path) -> int:
    """The number of earlier runs; an absent or unreadable file reads as none."""
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def record_run(path: Path, earlier: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(f"{earlier + 1}\n", encoding="utf-8")
    temp_path.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print how many times the welcome skill has run, and record this run."
    )
    parser.add_argument(
        "--peek", action="store_true", help="Print the count without recording a run."
    )
    args = parser.parse_args(argv)
    path = count_path(os.environ, Path.cwd())
    earlier = read_count(path)
    if not args.peek:
        record_run(path, earlier)
    print(earlier)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

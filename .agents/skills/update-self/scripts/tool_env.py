#!/usr/bin/env python3
"""Where this workspace's uv-managed tools live, and removal of copies that shadow them.

uv's tool directories follow ``$HOME``, and the programs that install tools run under two
of them: the image build's ``/root``, and ``/home/user`` on a live create (root's passwd
home at runtime). Left to that default a create installs a second copy of every tool under
``/home/user/.local``. No update refreshes that copy, and a login shell finds it first --
the desktop app's ``mngr exec`` and the terminal app both arrive through one -- so the
stale copy is what they run while the refreshed one reports success.

Two programs need this at different points in a workspace's life: the build
(``system/scripts/build_workspace.sh``, at image build and at a create) and the update
apply (``.agents/skills/update-self/scripts/update_environment.py``, on a workspace that
already exists). They differ only in how they name the installation to keep -- the build
pins it, the apply resolves it from ``PATH`` -- so both pass it in.

The apply is staged and run as a self-contained unit, so that an update cannot fail on a
divergence between the tree it came from and the tree it is landing. That rules out
importing across the two, and this file therefore exists twice, byte for byte:

    system/scripts/tool_env.py
    .agents/skills/update-self/scripts/tool_env.py

``system/scripts/tool_env_sync_test.py`` fails if they differ; edit one and copy it over
the other. They are kept identical rather than merely equivalent because the equivalent
versions did diverge, silently: one parsed a shebang by cutting at the first space and the
other by stripping first, so a ``#! /path`` spelling resolved to nothing and the cleanup
removed a tool environment while leaving its console script on PATH.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

# uv records how a tool was installed here, inside the tool's own directory. Its presence
# is what distinguishes a tool environment from a venv that merely holds a console script.
RECEIPT = "uv-receipt.toml"

# The vendored mngr distribution's name (its ``[project] name``), which is what uv names
# its environment directory -- not the ``mngr`` console script, which is the executable.
MNGR_TOOL_NAME = "imbue-mngr"
MNGR_EXECUTABLE = "mngr"

# The home every tool this workspace installs is reached through: the one the image build
# ran under, and the one whose bin directory is first on PATH in a login shell.
DEFAULT_TOOL_HOME = "/root"


def tool_home() -> Path:
    """The home whose ``.local`` holds the installations this workspace runs.

    ``TOOL_ENV_HOME`` overrides it, which is how the tests point the whole module at a
    temporary directory.
    """
    return Path(os.environ.get("TOOL_ENV_HOME") or DEFAULT_TOOL_HOME)


def tools_dir(home: Path) -> Path:
    """uv's tool-environment directory under ``home``."""
    return home / ".local" / "share" / "uv" / "tools"


def bin_dir(home: Path) -> Path:
    """uv's console-script directory under ``home``."""
    return home / ".local" / "bin"


def tool_location(script: Path, tool_name: str) -> tuple[Path, Path] | None:
    """``(tool_dir, bin_dir)`` for the uv tool that owns console ``script``, else ``None``.

    Resolved from the script's shebang rather than asked of uv, for two reasons: uv's
    default tool dir follows ``$HOME``, which is not the one the workspace was built
    under, and a venv console script must not masquerade as a tool.
    """
    try:
        shebang = script.read_text(errors="replace").split("\n", 1)[0]
    except OSError:
        return None
    if not shebang.startswith("#!"):
        return None
    # ``strip`` before splitting, so the ``#! /path`` spelling resolves like ``#!/path``.
    interpreter = shebang[2:].strip().split(" ", 1)[0]
    if not interpreter:
        return None
    parents = Path(interpreter).parents
    if len(parents) < 3:
        return None
    # uv writes ``#!<tool_dir>/<tool_name>/bin/python``, so the tool dir is three up.
    tool_dir = parents[2]
    if not (tool_dir / tool_name / RECEIPT).is_file():
        return None
    return tool_dir, script.parent


def remove_shadowing_mngr_installs(
    canonical_tools: Path, homes: Sequence[Path]
) -> list[Path]:
    """Delete mngr tool environments outside ``canonical_tools``, and the scripts into them.

    ``canonical_tools`` is the tool directory to keep, already identified by its caller --
    pinned by the build, resolved from ``PATH`` by the apply. Nothing is removed unless
    that installation is actually present, so a failed or half-finished install never
    leaves the workspace with no mngr at all.

    Directories are compared after ``resolve()``: a home reached as ``/root/`` or through a
    symlink is the installation being kept, not a shadow of it, and deleting it would
    destroy exactly what this is protecting. A console script goes only when it resolves
    into the environment being removed; one already pointing at the kept installation is a
    working shim, and removing the environment while stranding the script would leave a
    ``mngr`` on PATH with a dead interpreter -- worse than the stale copy it replaced.

    Returns what was removed.
    """
    canonical = canonical_tools.resolve()
    if not (canonical / MNGR_TOOL_NAME).is_dir():
        return []
    removed: list[Path] = []
    for home in homes:
        tools = tools_dir(home)
        stale_env = tools / MNGR_TOOL_NAME
        try:
            is_present = stale_env.is_dir()
        except PermissionError:
            # A home this process cannot read (a non-root run) holds nothing to remove.
            is_present = False
        if not is_present or tools.resolve() == canonical:
            continue
        shim = bin_dir(home) / MNGR_EXECUTABLE
        shim_location = tool_location(shim, MNGR_TOOL_NAME)
        if shim_location is not None and shim_location[0].resolve() == tools.resolve():
            shim.unlink()
            removed.append(shim)
        shutil.rmtree(stale_env)
        removed.append(stale_env)
    return removed


def _drop_shadowing_mngr() -> list[Path]:
    """The build's call: keep the pinned installation, sweep the home the build runs under."""
    home = os.environ.get("HOME")
    return remove_shadowing_mngr_installs(
        tools_dir(tool_home()), [Path(home)] if home else []
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manage this workspace's uv tool installations."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser(
        "drop-shadowing-mngr",
        help="Remove any mngr tool install outside the pinned one that would shadow it.",
    )
    parser.parse_args(argv)
    for removed in _drop_shadowing_mngr():
        sys.stderr.write(
            f"[tool-env] removed {removed}, which shadowed {tool_home()}/.local\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

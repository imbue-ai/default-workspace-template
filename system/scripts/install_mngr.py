#!/usr/bin/env python3
"""Install the vendored mngr as a uv tool, with the plugins the manifest assigns it.

The base package and its plugins go in ONE ``uv tool install``. Installing the base alone
rebuilds the tool environment from that package and drops every extra, so an install
followed by a separate ``mngr plugin add`` leaves the tool plugin-less in between -- and a
run that dies in that window leaves it that way for good. Such an mngr cannot resolve
``[agent_types.claude]``, so ``mngr create --template chat`` fails on the ``chat``
template's ``output_style``; that create is how the desktop app starts an update, so the
workspace stops being updatable from the app at all.

This exists as a program rather than a documented command line because every hazard in it
is a shell hazard: an empty plugin list that ``set -e`` cannot see because the substitution
that produced it swallowed the lister's exit status, an install that lands under the wrong
``$HOME`` because uv's tool directories follow it, strict mode leaking into whatever shell
sourced the pin. None of those survive being written in Python, and a program can be
tested where a snippet in AGENTS.md cannot.

The update apply does its own install (``update_environment._reinstall_tool``) and is not
a caller: it unions the extras already in the tool's receipt with the manifest's, which is
how a release that adds a plugin reaches a workspace that predates it. That is a different
operation, not a copy of this one.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import tool_env
from list_mngr_plugins import plugin_paths_for_tool

# The vendored mngr monorepo's installable package. Installing the tree root instead fails
# with a setuptools flat-layout error.
MNGR_SOURCE_DIR = "system/vendor/mngr/libs/mngr"

# How the plugin manifest names the mngr tool's own plugin set.
MNGR_PLUGIN_KEY = "mngr"

MANIFEST_PATH = "system/config/mngr_plugins.toml"


class NoPluginsListed(Exception):
    """The manifest assigned the mngr tool no plugins, so an install would strand it."""


def build_install_command(repo_root: Path, plugin_paths: Sequence[str]) -> list[str]:
    """The single ``uv tool install`` that lands mngr and its plugins.

    ``--reinstall`` because a from-scratch rebuild is what the apply does too
    (``_reinstall_tool``), and because an environment carrying extras this run does not
    name should lose them rather than keep them silently.
    """
    if not plugin_paths:
        raise NoPluginsListed(
            f"{MANIFEST_PATH} lists no plugins for the '{MNGR_PLUGIN_KEY}' tool. Installing "
            "the base package alone leaves an mngr that cannot parse [agent_types.*], which "
            "breaks `mngr create --template chat` and with it the app's update path."
        )
    command = ["uv", "tool", "install", "-e", str(repo_root / MNGR_SOURCE_DIR)]
    for path in plugin_paths:
        command += ["--with-editable", str(repo_root / path)]
    return command + ["--reinstall"]


def install_mngr(repo_root: Path) -> list[str]:
    """Run the install; return the command that was run."""
    manifest = (repo_root / MANIFEST_PATH).read_text()
    command = build_install_command(
        repo_root, plugin_paths_for_tool(manifest, MNGR_PLUGIN_KEY)
    )
    subprocess.run(command, cwd=repo_root, check=True)
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo-root",
        default=os.environ.get("REPO_ROOT", "."),
        help="The workspace root to install from (default: $REPO_ROOT, else cwd).",
    )
    args = parser.parse_args(argv)
    # The install must land in the pinned tool directories, not wherever $HOME points: an
    # agent running this by hand has HOME=/home/user while the mngr being repaired is the
    # one on PATH under the pinned home. Done here rather than asked of the caller,
    # because a caller who forgets gets a success message and an unchanged broken tool.
    # ``setdefault``, so build_workspace.sh's own pin (already exported) still wins.
    home = tool_env.tool_home()
    os.environ.setdefault("UV_TOOL_DIR", str(tool_env.tools_dir(home)))
    os.environ.setdefault("UV_TOOL_BIN_DIR", str(tool_env.bin_dir(home)))
    try:
        install_mngr(Path(args.repo_root))
    except NoPluginsListed as error:
        sys.stderr.write(f"install_mngr: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

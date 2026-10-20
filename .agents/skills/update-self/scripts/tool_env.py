#!/usr/bin/env python3
"""Where this workspace's uv-managed tools live, and removal of copies that shadow them
or that the workspace no longer runs.

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

``system/scripts/update_self_mirrors_sync_test.py`` fails if they differ; edit one and copy it over
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
import tomllib
from collections.abc import Sequence
from pathlib import Path

# uv records how a tool was installed here, inside the tool's own directory. Its presence
# is what distinguishes a tool environment from a venv that merely holds a console script.
RECEIPT = "uv-receipt.toml"

# The mngr distribution's name (its ``[project] name``), which is what uv names
# its environment directory -- not the ``mngr`` console script, which is the executable.
MNGR_TOOL_NAME = "imbue-mngr"

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


def _shebang_interpreter(script: Path) -> Path | None:
    """The interpreter ``script``'s shebang names, else ``None``.

    Only the first line is read: a bin directory also holds large compiled programs.
    """
    try:
        with script.open("rb") as handle:
            first_line = handle.readline(4096)
    except OSError:
        return None
    shebang = first_line.decode(errors="replace").split("\n", 1)[0]
    if not shebang.startswith("#!"):
        return None
    # ``strip`` before splitting, so the ``#! /path`` spelling resolves like ``#!/path``.
    interpreter = shebang[2:].strip().split(" ", 1)[0]
    return Path(interpreter) if interpreter else None


def tool_location(script: Path, tool_name: str) -> tuple[Path, Path] | None:
    """``(tool_dir, bin_dir)`` for the uv tool that owns console ``script``, else ``None``.

    Resolved from the script's shebang rather than asked of uv, for two reasons: uv's
    default tool dir follows ``$HOME``, which is not the one the workspace was built
    under, and a venv console script must not masquerade as a tool.
    """
    interpreter = _shebang_interpreter(script)
    if interpreter is None:
        return None
    parents = interpreter.parents
    if len(parents) < 3:
        return None
    # uv writes ``#!<tool_dir>/<tool_name>/bin/python``, so the tool dir is three up.
    tool_dir = parents[2]
    if not (tool_dir / tool_name / RECEIPT).is_file():
        return None
    return tool_dir, script.parent


def remove_shadowing_installs(
    canonical_tools: Path, homes: Sequence[Path], tool_name: str
) -> list[Path]:
    """Delete ``tool_name``'s environments outside ``canonical_tools``, and the scripts into them.

    ``canonical_tools`` is the tool directory to keep, already identified by its caller --
    pinned by the build, resolved from ``PATH`` by the apply. Nothing is removed unless
    that installation is actually present, so a failed or half-finished install never
    leaves the workspace without the tool at all.

    Directories are compared after ``resolve()``: a home reached as ``/root/`` or through a
    symlink is the installation being kept, not a shadow of it, and deleting it would
    destroy exactly what this is protecting. Every console script in the home's bin
    directory that resolves into the environment being removed goes with it -- a tool can
    install several, and stranding any would leave it on PATH with a dead interpreter,
    worse than the stale copy it replaced. One already pointing at the kept installation is
    a working shim and stays.

    Returns what was removed.
    """
    canonical = canonical_tools.resolve()
    if not (canonical / tool_name).is_dir():
        return []
    removed: list[Path] = []
    for home in homes:
        tools = tools_dir(home)
        stale_env = tools / tool_name
        try:
            is_present = stale_env.is_dir()
        except PermissionError:
            # A home this process cannot read (a non-root run) holds nothing to remove.
            is_present = False
        if not is_present or tools.resolve() == canonical:
            continue
        removed.extend(_remove_scripts_into(bin_dir(home), stale_env.resolve()))
        shutil.rmtree(stale_env)
        removed.append(stale_env)
    return removed


def _remove_scripts_into(scripts_dir: Path, environment: Path) -> list[Path]:
    """Unlink every console script in ``scripts_dir`` whose interpreter lives in
    ``environment`` (already resolved); return them.

    Only the environment directory is resolved, never the interpreter itself: uv's
    ``bin/python`` is a symlink out to the base interpreter.
    """
    if not scripts_dir.is_dir():
        return []
    removed: list[Path] = []
    for script in sorted(scripts_dir.iterdir()):
        interpreter = _shebang_interpreter(script)
        if interpreter is None or len(interpreter.parents) < 2:
            continue
        if interpreter.parents[1].resolve() == environment:
            script.unlink()
            removed.append(script)
    return removed


# CLEANUP: remove this function, its two callers (build_workspace.sh's `drop-app-tools` and
# the update apply) and its tests once no supported workspace or baked image can still carry
# the per-app tool installs that builds before the single-install change made.
def remove_app_tool_installs(homes: Sequence[Path], apps_dir: Path) -> list[Path]:
    """Delete every tool environment under ``homes`` that uv installed editable from a
    directory in ``apps_dir``, and every console script into it; return what was removed.

    Every app runs from the workspace venv, so such a tool is a second, unmaintained
    install. An environment is an app's tool when its receipt names its own package as an
    editable install from under ``apps_dir``, so a user-built app goes too and nothing
    installed any other way is touched. Paths are
    compared after ``resolve()``, because the receipt spells the checkout the way the build
    reached it.
    """
    apps = apps_dir.resolve()
    removed: list[Path] = []
    for home in homes:
        tools = tools_dir(home)
        try:
            environments = sorted(tools.iterdir()) if tools.is_dir() else []
        except PermissionError:
            # A home this process cannot read (a non-root run) holds nothing to remove.
            environments = []
        for environment in environments:
            if not _is_editable_install_under(environment, apps):
                continue
            removed.extend(_remove_scripts_into(bin_dir(home), environment.resolve()))
            shutil.rmtree(environment)
            removed.append(environment)
    return removed


def _is_editable_install_under(environment: Path, directory: Path) -> bool:
    """Whether ``environment``'s receipt installs its own package editable from under
    ``directory`` (already resolved)."""
    try:
        receipt = tomllib.loads((environment / RECEIPT).read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return False
    tool = receipt.get("tool")
    requirements = tool.get("requirements", []) if isinstance(tool, dict) else []
    own_name = _canonical_name(environment.name)
    for requirement in requirements if isinstance(requirements, list) else []:
        if not isinstance(requirement, dict):
            continue
        source = requirement.get("editable")
        if _canonical_name(
            str(requirement.get("name", ""))
        ) != own_name or not isinstance(source, str):
            continue
        return Path(source).resolve().is_relative_to(directory)
    return False


def _canonical_name(name: str) -> str:
    """A distribution name normalized the way uv names its environment directory."""
    return name.replace("_", "-").lower()


def _drop_app_tools(repo_root: Path) -> list[Path]:
    """The build's call: sweep the pinned home and the home the build runs under."""
    home = os.environ.get("HOME")
    homes = [tool_home(), *([Path(home)] if home else [])]
    return remove_app_tool_installs(homes, repo_root / "system" / "apps")


def _drop_shadowing_mngr() -> list[Path]:
    """The build's call: keep the pinned installation, sweep the home the build runs under."""
    home = os.environ.get("HOME")
    return remove_shadowing_installs(
        tools_dir(tool_home()),
        [Path(home)] if home else [],
        MNGR_TOOL_NAME,
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
    drop_app_tools = subcommands.add_parser(
        "drop-app-tools",
        help="Remove the per-app uv tool installs older builds made beside the workspace venv.",
    )
    drop_app_tools.add_argument("--repo-root", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "drop-app-tools":
        for removed in _drop_app_tools(args.repo_root):
            sys.stderr.write(
                f"[tool-env] removed {removed}, an app tool the workspace venv replaces\n"
            )
        return 0
    for removed in _drop_shadowing_mngr():
        sys.stderr.write(
            f"[tool-env] removed {removed}, which shadowed {tool_home()}/.local\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

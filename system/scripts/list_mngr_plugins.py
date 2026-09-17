#!/usr/bin/env python3
"""Print ``uv tool install`` arguments for mngr, or for the plugins one tool registers.

One argument per line, ready to collect into a shell array. ``--base`` prints the
arguments that install ``imbue-mngr`` itself; ``--tool`` prints those for every plugin
``system/config/mngr_plugins.toml`` assigns to that tool, in file order; ``--pin``
prints where mngr comes from.

Where mngr comes from is decided by ``pyproject.toml``'s ``[tool.uv.sources]`` entry
for ``imbue-mngr`` -- the one place that lives -- so the tool environments and the
workspace venv can never disagree:

    imbue-mngr = { git = "https://github.com/imbue-ai/mngr", rev = "<commit>", subdirectory = "libs/mngr" }

which yields git requirements:

    imbue-mngr @ git+https://github.com/imbue-ai/mngr@<commit>#subdirectory=libs/mngr
    --with
    imbue-mngr-claude @ git+https://github.com/imbue-ai/mngr@<commit>#subdirectory=libs/mngr_claude

The manifest contributes only each plugin's package name and its subdirectory in the
mngr repo; every plugin's location derives from ``imbue-mngr``'s.
"""

import argparse
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

MANIFEST_PATH = "system/config/mngr_plugins.toml"
PYPROJECT_PATH = "pyproject.toml"
MNGR_PACKAGE = "imbue-mngr"
MNGR_SUBDIRECTORY = "libs/mngr"


class MngrPinError(Exception):
    """The mngr source could not be read from pyproject.toml."""


@dataclass(frozen=True)
class GitPin:
    """mngr comes from a git repo at a fixed commit."""

    git_url: str
    rev: str

    def requirement(self, package: str, subdirectory: str) -> str:
        return f"{package} @ git+{self.git_url}@{self.rev}#subdirectory={subdirectory}"


def read_mngr_source(pyproject_text: str) -> GitPin:
    """The pin ``[tool.uv.sources]`` gives ``imbue-mngr``."""
    source = (
        tomllib.loads(pyproject_text)
        .get("tool", {})
        .get("uv", {})
        .get("sources", {})
        .get(MNGR_PACKAGE)
    )
    if isinstance(source, dict) and "git" in source and "rev" in source:
        return GitPin(git_url=str(source["git"]), rev=str(source["rev"]))
    raise MngrPinError(
        f"{PYPROJECT_PATH} must give {MNGR_PACKAGE} in [tool.uv.sources] as "
        '{ git = "...", rev = "<commit>", subdirectory = "libs/mngr" }'
    )


def base_arguments(source: GitPin) -> list[str]:
    """The arguments that install ``imbue-mngr`` itself."""
    return [source.requirement(MNGR_PACKAGE, MNGR_SUBDIRECTORY)]


def plugin_arguments_for_tool(
    manifest_text: str, source: GitPin, tool: str
) -> list[str]:
    """The ``--with`` arguments for the manifest's plugins assigned to ``tool``."""
    manifest = tomllib.loads(manifest_text)
    arguments: list[str] = []
    for entry in manifest.get("plugins", []):
        if tool not in entry.get("tools", []):
            continue
        arguments += [
            "--with",
            source.requirement(str(entry["package"]), str(entry["subdirectory"])),
        ]
    return arguments


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument(
        "--base",
        action="store_true",
        help="print the arguments that install imbue-mngr itself",
    )
    what.add_argument(
        "--pin",
        action="store_true",
        help="print where mngr comes from: '<git url> <commit>'",
    )
    what.add_argument(
        "--tool",
        help="print the plugin arguments for this tool: mngr or system-interface",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="The workspace root the manifest and pyproject.toml are read under (default: cwd).",
    )
    args = parser.parse_args(argv)
    root = Path(args.repo_root).resolve()
    source = read_mngr_source((root / PYPROJECT_PATH).read_text())
    if args.pin:
        lines = [f"{source.git_url} {source.rev}"]
    elif args.base:
        lines = base_arguments(source)
    else:
        lines = plugin_arguments_for_tool(
            (root / MANIFEST_PATH).read_text(), source, args.tool
        )
    for line in lines:
        sys.stdout.write(f"{line}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

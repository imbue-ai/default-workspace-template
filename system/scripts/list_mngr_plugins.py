#!/usr/bin/env python3
"""Print ``uv tool install`` arguments for mngr, or for the plugins one tool registers.

One argument per line, ready to collect into a shell array. ``--base`` prints the
arguments that install ``imbue-mngr`` itself; ``--tool`` prints those for every plugin
``system/config/mngr_plugins.toml`` assigns to that tool, in file order; ``--pin``
prints where mngr comes from.

Where mngr comes from is decided by ``pyproject.toml``'s ``[tool.uv.sources]`` entry
for ``imbue-mngr`` -- the one place that lives -- so the tool environments and the
workspace venv can never disagree. It has two shapes:

    imbue-mngr = { git = "https://github.com/imbue-ai/mngr", rev = "<commit>", subdirectory = "libs/mngr" }

the pin every shipped image runs, which yields git requirements:

    imbue-mngr @ git+https://github.com/imbue-ai/mngr@<commit>#subdirectory=libs/mngr
    --with
    imbue-mngr-claude @ git+https://github.com/imbue-ai/mngr@<commit>#subdirectory=libs/mngr_claude

or, when a checkout has been pointed at a local mngr tree for development or CI,

    imbue-mngr = { path = "system/vendor/mngr/libs/mngr", editable = true }

which yields editable installs from that tree:

    -e
    /home/user/workspace/system/vendor/mngr/libs/mngr
    --with-editable
    /home/user/workspace/system/vendor/mngr/libs/mngr_claude

The manifest contributes only each plugin's package name and its subdirectory in the
mngr repo; both shapes derive every plugin's location from ``imbue-mngr``'s.
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


@dataclass(frozen=True)
class LocalTree:
    """mngr comes from a local checkout, installed editable; ``root`` is the checkout's top."""

    root: Path


MngrSource = GitPin | LocalTree


def read_mngr_source(pyproject_text: str, repo_root: Path) -> MngrSource:
    """The source ``[tool.uv.sources]`` gives ``imbue-mngr``, as a :class:`GitPin` or :class:`LocalTree`."""
    source = tomllib.loads(pyproject_text).get("tool", {}).get("uv", {}).get("sources", {}).get(MNGR_PACKAGE)
    if isinstance(source, dict) and "git" in source and "rev" in source:
        return GitPin(git_url=str(source["git"]), rev=str(source["rev"]))
    if isinstance(source, dict) and "path" in source and source.get("editable") is True:
        lib = Path(str(source["path"]))
        if lib.parts[-len(Path(MNGR_SUBDIRECTORY).parts) :] != Path(MNGR_SUBDIRECTORY).parts:
            raise MngrPinError(f"{PYPROJECT_PATH}: a local imbue-mngr path must end in {MNGR_SUBDIRECTORY}, got {lib}")
        root = lib.parents[len(Path(MNGR_SUBDIRECTORY).parts) - 1]
        return LocalTree(root=root if root.is_absolute() else (repo_root / root).resolve())
    raise MngrPinError(
        f"{PYPROJECT_PATH} must give {MNGR_PACKAGE} in [tool.uv.sources] as either "
        '{ git = "...", rev = "<commit>", subdirectory = "libs/mngr" } or '
        '{ path = "<checkout>/libs/mngr", editable = true }'
    )


def base_arguments(source: MngrSource) -> list[str]:
    """The arguments that install ``imbue-mngr`` itself."""
    if isinstance(source, GitPin):
        return [source.requirement(MNGR_PACKAGE, MNGR_SUBDIRECTORY)]
    return ["-e", str(source.root / MNGR_SUBDIRECTORY)]


def plugin_arguments_for_tool(manifest_text: str, source: MngrSource, tool: str) -> list[str]:
    """The ``--with`` / ``--with-editable`` arguments for the manifest's plugins assigned to ``tool``."""
    manifest = tomllib.loads(manifest_text)
    arguments: list[str] = []
    for entry in manifest.get("plugins", []):
        if tool not in entry.get("tools", []):
            continue
        package, subdirectory = str(entry["package"]), str(entry["subdirectory"])
        if isinstance(source, GitPin):
            arguments += ["--with", source.requirement(package, subdirectory)]
        else:
            arguments += ["--with-editable", str(source.root / subdirectory)]
    return arguments


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--base", action="store_true", help="print the arguments that install imbue-mngr itself")
    what.add_argument("--pin", action="store_true", help="print where mngr comes from: '<git url> <commit>' or '<checkout>'")
    what.add_argument("--tool", help="print the plugin arguments for this tool: mngr or system-interface")
    parser.add_argument(
        "--repo-root",
        default=".",
        help="The workspace root the manifest and pyproject.toml are read under (default: cwd).",
    )
    args = parser.parse_args(argv)
    root = Path(args.repo_root).resolve()
    source = read_mngr_source((root / PYPROJECT_PATH).read_text(), root)
    if args.pin:
        lines = [f"{source.git_url} {source.rev}" if isinstance(source, GitPin) else str(source.root)]
    elif args.base:
        lines = base_arguments(source)
    else:
        lines = plugin_arguments_for_tool((root / MANIFEST_PATH).read_text(), source, args.tool)
    for line in lines:
        sys.stdout.write(f"{line}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

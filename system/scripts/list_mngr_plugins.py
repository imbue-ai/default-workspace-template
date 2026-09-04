#!/usr/bin/env python3
"""Print the PEP 508 requirement for mngr, or for the plugins one tool registers.

``--pin`` prints the pinned repo URL and commit; ``--base`` prints the requirement for
``imbue-mngr`` itself; ``--tool`` prints one
requirement per plugin ``system/config/mngr_plugins.toml`` assigns to that tool, in
file order. Either way each line is ready for ``uv tool install``:

    imbue-mngr-claude @ git+https://github.com/imbue-ai/mngr@<rev>#subdirectory=libs/mngr_claude

The repo and the commit come from ``pyproject.toml``'s ``[tool.uv.sources]`` entry
for ``imbue-mngr`` -- the one place the mngr pin lives -- so the tool environments and
the workspace venv can never resolve different commits. The manifest contributes only
the package name and its subdirectory in that repo.
"""

import argparse
import sys
import tomllib
from pathlib import Path

MANIFEST_PATH = "system/config/mngr_plugins.toml"
PYPROJECT_PATH = "pyproject.toml"
MNGR_PACKAGE = "imbue-mngr"


class MngrPinError(Exception):
    """The mngr pin could not be read from pyproject.toml."""


def read_mngr_pin(pyproject_text: str) -> tuple[str, str]:
    """The ``(git_url, rev)`` that ``[tool.uv.sources]`` pins ``imbue-mngr`` to."""
    source = tomllib.loads(pyproject_text).get("tool", {}).get("uv", {}).get("sources", {}).get(MNGR_PACKAGE)
    if not isinstance(source, dict) or "git" not in source or "rev" not in source:
        raise MngrPinError(
            f"{PYPROJECT_PATH} must pin {MNGR_PACKAGE} in [tool.uv.sources] as "
            '{ git = "...", rev = "<commit>", subdirectory = "libs/mngr" }'
        )
    return str(source["git"]), str(source["rev"])


def requirement(package: str, git_url: str, rev: str, subdirectory: str) -> str:
    return f"{package} @ git+{git_url}@{rev}#subdirectory={subdirectory}"


def base_requirement(pyproject_text: str) -> str:
    git_url, rev = read_mngr_pin(pyproject_text)
    source = tomllib.loads(pyproject_text)["tool"]["uv"]["sources"][MNGR_PACKAGE]
    return requirement(MNGR_PACKAGE, git_url, rev, str(source["subdirectory"]))


def plugin_requirements_for_tool(manifest_text: str, pyproject_text: str, tool: str) -> list[str]:
    """The manifest's plugins whose ``tools`` list names ``tool``, as requirements, in file order."""
    git_url, rev = read_mngr_pin(pyproject_text)
    manifest = tomllib.loads(manifest_text)
    return [
        requirement(str(entry["package"]), git_url, rev, str(entry["subdirectory"]))
        for entry in manifest.get("plugins", [])
        if tool in entry.get("tools", [])
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--base", action="store_true", help="print the requirement for imbue-mngr itself")
    what.add_argument("--pin", action="store_true", help="print the pinned repo URL and commit, space-separated")
    what.add_argument("--tool", help="print the plugin requirements for this tool: mngr or system-interface")
    parser.add_argument(
        "--repo-root",
        default=".",
        help="The workspace root the manifest and pyproject.toml are read under (default: cwd).",
    )
    args = parser.parse_args(argv)
    root = Path(args.repo_root)
    pyproject_text = (root / PYPROJECT_PATH).read_text()
    if args.pin:
        lines = [" ".join(read_mngr_pin(pyproject_text))]
    elif args.base:
        lines = [base_requirement(pyproject_text)]
    else:
        lines = plugin_requirements_for_tool((root / MANIFEST_PATH).read_text(), pyproject_text, args.tool)
    for line in lines:
        sys.stdout.write(f"{line}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

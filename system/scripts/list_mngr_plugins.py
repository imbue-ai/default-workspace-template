#!/usr/bin/env python3
"""Print the plugin paths ``system/config/mngr_plugins.toml`` assigns to one tool.

One path per line, relative to the workspace root, for ``build_workspace.sh``
to feed ``uv tool install --with-editable`` and ``mngr plugin add --path``.
The update-self apply reads the same file itself.
"""

import argparse
import sys
import tomllib
from pathlib import Path

MANIFEST_PATH = "system/config/mngr_plugins.toml"


def plugin_paths_for_tool(manifest_text: str, tool: str) -> list[str]:
    """The manifest's plugin paths whose ``tools`` list names ``tool``, in file order."""
    manifest = tomllib.loads(manifest_text)
    return [
        str(entry["path"])
        for entry in manifest.get("plugins", [])
        if tool in entry.get("tools", [])
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", required=True, help="mngr or system-interface")
    parser.add_argument(
        "--repo-root",
        default=".",
        help="The workspace root the manifest is read under (default: cwd).",
    )
    args = parser.parse_args(argv)
    manifest = Path(args.repo_root) / MANIFEST_PATH
    for path in plugin_paths_for_tool(manifest.read_text(), args.tool):
        sys.stdout.write(f"{path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

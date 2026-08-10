#!/usr/bin/env python3
"""Generate `template.toml` for a snapshot being assembled.

Called by `build_template.sh` after it has assembled the tree. Writes the
machine-readable half of the manifest: identity, the derivation recipe, and the
lineage inherited from whatever manifest this snapshot overrides.

Everything the publishing worker must still supply -- the recipe's `exclude`
and `modification_rules`, the structured `[requirements]`, and the `[environment]`
declarations -- is emitted as an empty section with a prompting comment. Those
all have empty defaults, so the generated file is valid TOML the moment it is
written and `validate_template.py` can gate on it immediately; the forcing
function for filling them in is the task instructions plus the markdown/TOML
agreement check, which fails if the worker writes a `requires_` line in
`template.md` without its counterpart here.

Pure standard library: it runs in the assembly worker's post-reset worktree,
where there is no venv, so it must work under a bare `python3`. Strings are
emitted with `json.dumps`, whose escaping is a valid subset of TOML's
basic-string escaping -- and any mistake is caught immediately, because
`build_template.sh` validates the file it just wrote.

Usage (from the assembled repo root):

    python3 write_template_manifest.py --slug S --title T --description D \\
        --version v1 --include PATH [--include PATH ...] \\
        [--data-include PATH ...] [--previous-manifest PATH] --output PATH
"""

import argparse
import json
import sys
import tomllib
from pathlib import Path


class WriteTemplateManifestError(Exception):
    """Base exception for manifest generation failures."""


class PreviousManifestUnreadableError(WriteTemplateManifestError, ValueError):
    """Raised when the manifest being overridden cannot be parsed.

    Fatal rather than ignored: silently dropping an unreadable predecessor
    would lose the lineage chain, which is the only record of what this
    snapshot replaced.
    """

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"Cannot read the previous manifest at {path}: {reason}")


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _toml_string_array(values: list[str]) -> str:
    if not values:
        return "[]"
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def read_inherited_lineage(previous_manifest_path: Path) -> list[dict[str, str]]:
    """The lineage entries a new manifest inherits from the one it overrides.

    Mirrors `env_converge.template_manifest.lineage_after_override`, in
    stdlib form because this script cannot import the schema module: the
    predecessor's own chain first (oldest first), then the predecessor itself
    when its `[origin]` gives an address to record.
    """
    if not previous_manifest_path.is_file():
        return []
    try:
        previous = tomllib.loads(previous_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise PreviousManifestUnreadableError(previous_manifest_path, str(e)) from e

    inherited: list[dict[str, str]] = []
    for entry in previous.get("lineage", []):
        inherited.append(
            {
                key: str(entry[key])
                for key in ("slug", "repo_url", "commit", "used_on")
                if key in entry
            }
        )

    origin = previous.get("origin")
    identity = previous.get("template", {})
    if isinstance(origin, dict) and "repo_url" in origin and "commit" in origin:
        entry = {
            "slug": str(identity.get("slug", "unknown")),
            "repo_url": str(origin["repo_url"]),
            "commit": str(origin["commit"]),
        }
        if "adopted_on" in origin:
            entry["used_on"] = str(origin["adopted_on"])
        inherited.append(entry)
    return inherited


def render_manifest(
    slug: str,
    title: str,
    description: str,
    version: str,
    manifest_format: str,
    thumbnail: str,
    include: list[str],
    data_include: list[str],
    lineage: list[dict[str, str]],
) -> str:
    lines = [
        "# Machine-readable manifest for this template. The sibling",
        "# template.md holds the prose, the Requirements list, and the two",
        "# append-only history logs; this file is authoritative for everything",
        "# below. Both are generated together -- validate_template.py fails if",
        "# they disagree.",
        f"format = {_toml_string(manifest_format)}",
        "",
        "[template]",
        f"slug = {_toml_string(slug)}",
        f"title = {_toml_string(title)}",
        f"description = {_toml_string(description)}",
        f"thumbnail = {_toml_string(thumbnail)}",
        f"version = {_toml_string(version)}",
        "",
        "# How this template is DERIVED from the workspace it came from. An",
        "# update re-runs this recipe rather than diffing two repos, which is what",
        "# keeps an exclusion excluded even though the thing still exists upstream.",
        "[recipe]",
        f"include = {_toml_string_array(include)}",
        f"data_include = {_toml_string_array(data_include)}",
        "# FILL IN: one entry per deliberate exclusion (paths left out, and",
        "# features stripped from an included path). Leave [] if nothing was.",
        "exclude = []",
        "# FILL IN: one entry per published-version modification, stated as a RULE",
        "# and NEVER the removed value -- the point of a modification is that the",
        '# value does not ship. e.g. "replace the hardcoded team channel with a',
        '# neutral default". Leave [] if there were none.',
        "modification_rules = []",
        "",
        "# Everything an adopter must deal with before this is really theirs.",
        "# One list -- but each entry's KIND says how it is handled, because the",
        "# two are handled at different times by different mechanisms:",
        "#",
        "#   permission / secret / llm = ACTIVATION. The adopting agent acts on",
        "#   these FIRST and BY ITSELF, initiating each latchkey permission",
        "#   request before asking the user anything. Every one of them must have",
        "#   a matching requires_ line in template.md and vice versa -- the",
        "#   validator checks that, because an adopter once never got prompted",
        "#   for a permission the app needed.",
        "#",
        "#   adaptation = worked through INTERACTIVELY with the user afterwards.",
        "#   Prose on both sides, so it is not cross-checked.",
        "#",
        "# FILL IN, e.g.:",
        "#   [[requirements.permission]]",
        '#   scope = "slack-api"',
        '#   permission = "slack-read-all"',
        "#",
        "#   [[requirements.secret]]",
        '#   name = "SLACK_SIGNING_SECRET"',
        "#",
        "#   [requirements.llm]",
        '#   method = "keyed"   # keyed (ANTHROPIC_API_KEY) | keyless (claude -p)',
        "#",
        "#   [[requirements.adaptation]]",
        '#   summary = "the digest channel is hardcoded"',
        '#   resolution = "ask the user which channel to watch"',
        "[requirements]",
        "",
        "# What the included code needs INSTALLED. apt takes bare names: versions",
        "# are a function of the apt snapshot timestamp, so replaying names at the",
        "# adopter's timestamp yields versions consistent with the rest of their",
        "# environment. The other sources are not snapshot-pinned, so for them the",
        "# recorded version IS the pin (name = version).",
        "#",
        "# FILL IN from what the included code actually needs, e.g.:",
        '#   apt = ["poppler-utils"]',
        "#   [environment.npm_global]",
        '#   "@slack/cli" = "2.1.0"',
        "#   [environment.uv_tools]",
        '#   yt-dlp = "2026.7.1"',
        "#   [environment.cargo_crates]",
        '#   fd-find = "9.0.0"',
        "#",
        "# For an install with no package database (a URL-fetched binary, a browser),",
        "# ship a system/scripts/env.d/<NNNN>-<slug>-<name>.sh unit with NNNN >= 2000,",
        "# include it in the recipe above, and list it in env_d_units.",
        "[environment]",
        "apt = []",
        "env_d_units = []",
    ]

    if lineage:
        lines.extend(
            [
                "",
                "# Templates this mind used on the way to this one, oldest first.",
                "# A new manifest overrides its predecessor rather than accumulating",
                "# beside it; the commit hash is what keeps the superseded manifest",
                "# retrievable in the repo where it is authoritative.",
            ]
        )
        for entry in lineage:
            lines.append("")
            lines.append("[[lineage]]")
            for key in ("slug", "repo_url", "commit", "used_on"):
                if key in entry:
                    lines.append(f"{key} = {_toml_string(entry[key])}")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--format", dest="manifest_format", required=True)
    parser.add_argument("--thumbnail", required=True)
    parser.add_argument("--include", action="append", default=[])
    parser.add_argument("--data-include", action="append", default=[])
    parser.add_argument(
        "--previous-manifest",
        default="",
        help="The template.toml being overridden, staged before the reset",
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    lineage: list[dict[str, str]] = []
    if args.previous_manifest:
        lineage = read_inherited_lineage(Path(args.previous_manifest))

    Path(args.output).write_text(
        render_manifest(
            slug=args.slug,
            title=args.title,
            description=args.description,
            version=args.version,
            manifest_format=args.manifest_format,
            thumbnail=args.thumbnail,
            include=args.include,
            data_include=args.data_include,
            lineage=lineage,
        ),
        encoding="utf-8",
    )
    if lineage:
        print(
            f"write_template_manifest: carried {len(lineage)} lineage entr"
            f"{'y' if len(lineage) == 1 else 'ies'} forward",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Check the frontmatter the Skills app reads off every skill the template ships.

Each is marked as Imbue's (``metadata.author: imbue``): the Skills app lists a skill by that author, so one the
template ships without it reads as made in the user's own workspace. A ``metadata.audience``, where one is given, is
``system``, the only value that means anything (the agents' own procedure, kept behind the scenes); any other spelling
would silently read as a skill for people. Run by the template repository's CI alone (its ``skill-authors`` job), never
in a workspace made from the template, which holds skills by other authors. The vendored imbue-studio skill's author
is the mngr repository's to mark, and its own test there requires it.

    check_skill_authors.py                     # check the checkout in the current directory
    check_skill_authors.py --repo-root <dir>   # check another checkout
"""

import argparse
import sys
from pathlib import Path

from vendor_mngr_skill import SKILL_VENDORED_PATH

SKILLS_ROOT = Path(".agents")
SKILL_FILENAME = "SKILL.md"
STUDIO_AUTHOR = "imbue"
SYSTEM_AUDIENCE = "system"
_FRONTMATTER_DELIMITER = "---"


def frontmatter_field(text: str, field: str) -> str | None:
    """The value a SKILL.md's frontmatter gives ``field``, read line by line as mngr's skill reader does (so a key of
    the ``metadata`` map counts); None when it gives none."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        return None
    for line in lines[1:]:
        if line.strip() == _FRONTMATTER_DELIMITER:
            return None
        key, separator, value = line.partition(":")
        if separator and key.strip() == field:
            return value.strip().strip('"')
    return None


def _skill_files(root: Path) -> list[Path]:
    return sorted((root / SKILLS_ROOT).rglob(SKILL_FILENAME))


def skills_not_by_imbue(root: Path) -> list[Path]:
    """Every SKILL.md under .agents, relative to ``root``, whose author is not imbue, the vendored skill aside."""
    vendored = root / SKILL_VENDORED_PATH
    return [
        path.relative_to(root)
        for path in _skill_files(root)
        if path != vendored
        and frontmatter_field(path.read_text(encoding="utf-8"), "author")
        != STUDIO_AUTHOR
    ]


def skills_with_an_unknown_audience(root: Path) -> list[Path]:
    """Every SKILL.md under .agents, relative to ``root``, that gives an audience other than system."""
    return [
        path.relative_to(root)
        for path in _skill_files(root)
        if frontmatter_field(path.read_text(encoding="utf-8"), "audience")
        not in (None, SYSTEM_AUDIENCE)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo-root", default=".", help="the template checkout (default: cwd)"
    )
    args = parser.parse_args(argv)
    root = Path(args.repo_root)
    unmarked = skills_not_by_imbue(root)
    unknown_audience = skills_with_an_unknown_audience(root)
    for paths, need in (
        (unmarked, f"`metadata.author: {STUDIO_AUTHOR}`"),
        (unknown_audience, f"`metadata.audience: {SYSTEM_AUDIENCE}`, or no audience"),
    ):
        if paths:
            print(f"These skills need {need} in their frontmatter:", file=sys.stderr)
            for path in paths:
                print(f"  {path}", file=sys.stderr)
    return 1 if unmarked or unknown_audience else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Check that every skill the template ships is marked as Imbue's: ``metadata.author: imbue`` in its frontmatter.

The Skills app lists a skill by that author, so one the template ships without it reads as made in the user's own
workspace. Run by the template repository's CI alone (its ``skill-authors`` job), never in a workspace made from the
template, which holds skills by other authors. The vendored imbue-studio skill is the mngr repository's to mark, and
its own test there requires the author.

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
_FRONTMATTER_DELIMITER = "---"


def frontmatter_author(text: str) -> str | None:
    """The author a SKILL.md's frontmatter names, read line by line as mngr's skill reader does; None for none."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        return None
    for line in lines[1:]:
        if line.strip() == _FRONTMATTER_DELIMITER:
            return None
        key, separator, value = line.partition(":")
        if separator and key.strip() == "author":
            return value.strip().strip('"')
    return None


def skills_not_by_imbue(root: Path) -> list[Path]:
    """Every SKILL.md under .agents, relative to ``root``, whose author is not imbue, the vendored skill aside."""
    vendored = root / SKILL_VENDORED_PATH
    return sorted(
        path.relative_to(root)
        for path in (root / SKILLS_ROOT).rglob(SKILL_FILENAME)
        if path != vendored
        and frontmatter_author(path.read_text(encoding="utf-8")) != STUDIO_AUTHOR
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo-root", default=".", help="the template checkout (default: cwd)"
    )
    args = parser.parse_args(argv)
    unmarked = skills_not_by_imbue(Path(args.repo_root))
    if not unmarked:
        return 0
    print(
        f"These skills need `metadata.author: {STUDIO_AUTHOR}` in their frontmatter:",
        file=sys.stderr,
    )
    for path in unmarked:
        print(f"  {path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

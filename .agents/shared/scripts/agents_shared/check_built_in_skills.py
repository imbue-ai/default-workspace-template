"""Fail when a skill in the template's tree lacks ``metadata.author: imbue``.

The marker says a skill ships with the template. Every skill in the template's own tree is
built in; a workspace's tree also holds its own skills and third parties', so only the
template's CI runs this:

    uv run --no-sync .agents/shared/scripts/agents_shared/check_built_in_skills.py

Exits 0 and prints ``ok`` when every skill carries the marker; exits 1 naming the ones that
do not.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from agents_shared.validate_skill import is_built_in_skill


def skills_without_the_author(skills_dir: Path) -> list[str]:
    return [
        skill_dir.name
        for skill_dir in sorted(skills_dir.glob("*/"))
        if not is_built_in_skill(skill_dir)
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that every skill in the template carries metadata.author: imbue."
    )
    parser.add_argument(
        "--skills-dir",
        type=Path,
        default=Path(".agents/skills"),
        help="The skills directory (default: .agents/skills).",
    )
    args = parser.parse_args(argv)
    missing = skills_without_the_author(args.skills_dir)
    if missing:
        sys.stderr.write(
            "These skills ship with the template but lack `metadata.author: imbue` in their "
            "SKILL.md frontmatter:\n" + "".join(f"  - {name}\n" for name in missing)
        )
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())

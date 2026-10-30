"""Which skills the workspace holds to its own checks.

A skill is the workspace's own when its SKILL.md frontmatter carries ``metadata.author: imbue``
(built in) or ``metadata.crystallized: true`` (made in this workspace). Any other skill came
from a third party and is kept as it came, so the import-cost, type and launch checks leave it
out (``validate_skill.is_workspace_skill``).

    uv run --no-sync .agents/shared/scripts/agents_shared/skill_ownership.py unmarked

prints each skill with Python that carries neither marker, one per line: the ones whose maker
the update worker decides (update-self's ``references/python-packaging-migration.md``).

    uv run --no-sync .agents/shared/scripts/agents_shared/skill_ownership.py check-built-in

exits 1 naming every skill without ``metadata.author: imbue``. Every skill in the template's
own tree is built in; a workspace's tree also holds its own skills and third parties', so only
the template's CI runs it.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from agents_shared.validate_skill import is_built_in_skill, is_workspace_skill


def _skill_dirs(skills_dir: Path) -> list[Path]:
    return sorted(skills_dir.glob("*/"))


def unmarked_skills_with_python(skills_dir: Path) -> list[str]:
    return [
        skill_dir.name
        for skill_dir in _skill_dirs(skills_dir)
        if not is_workspace_skill(skill_dir) and any(skill_dir.rglob("*.py"))
    ]


def skills_without_the_author(skills_dir: Path) -> list[str]:
    return [
        skill_dir.name
        for skill_dir in _skill_dirs(skills_dir)
        if not is_built_in_skill(skill_dir)
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Which skills the workspace holds to its own checks."
    )
    parser.add_argument(
        "--skills-dir",
        type=Path,
        default=Path(".agents/skills"),
        help="The skills directory (default: .agents/skills).",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser(
        "unmarked", help="List the skills with Python that carry neither marker."
    )
    subcommands.add_parser(
        "check-built-in",
        help="Fail naming every skill without metadata.author: imbue (the template's CI).",
    )
    args = parser.parse_args(argv)
    if args.command == "unmarked":
        for name in unmarked_skills_with_python(args.skills_dir):
            print(name)
        return 0
    missing = skills_without_the_author(args.skills_dir)
    if missing:
        sys.stderr.write(
            "These skills ship with the template but lack `metadata.author: imbue` in their "
            "SKILL.md frontmatter, so a workspace would treat them as a third party's and leave "
            "them out of its checks:\n" + "".join(f"  - {name}\n" for name in missing)
        )
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())

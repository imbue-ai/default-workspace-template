"""Tests for the check that every skill the template ships is marked as Imbue's."""

from __future__ import annotations

from pathlib import Path

import check_skill_authors
import pytest
from vendor_mngr_skill import SKILL_VENDORED_PATH


def _write(root: Path, relative_path: str | Path, text: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _skill(author_lines: str) -> str:
    return f"---\nname: a-skill\ndescription: Does a thing.\n{author_lines}---\n\n# A skill\n\nauthor: not frontmatter\n"


def test_the_author_is_read_from_the_metadata_map_or_the_top_level() -> None:
    assert (
        check_skill_authors.frontmatter_author(_skill("metadata:\n  author: imbue\n"))
        == "imbue"
    )
    assert check_skill_authors.frontmatter_author(_skill('author: "user"\n')) == "user"


def test_a_skill_without_frontmatter_or_an_author_names_none() -> None:
    assert (
        check_skill_authors.frontmatter_author("# Just a body\n\nauthor: imbue\n")
        is None
    )
    assert check_skill_authors.frontmatter_author(_skill("")) is None


def test_every_skill_not_marked_as_imbue_s_is_reported_except_the_vendored_one(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        ".agents/skills/marked/SKILL.md",
        _skill("metadata:\n  author: imbue\n"),
    )
    _write(
        tmp_path,
        ".agents/skills/by-a-user/SKILL.md",
        _skill("metadata:\n  author: user\n"),
    )
    _write(tmp_path, ".agents/skills/unmarked/SKILL.md", _skill(""))
    _write(tmp_path, ".agents/shared/worker/SKILL.md", _skill(""))
    _write(tmp_path, SKILL_VENDORED_PATH, _skill(""))

    assert check_skill_authors.skills_not_by_imbue(tmp_path) == [
        Path(".agents/shared/worker/SKILL.md"),
        Path(".agents/skills/by-a-user/SKILL.md"),
        Path(".agents/skills/unmarked/SKILL.md"),
    ]


def test_the_check_fails_naming_the_unmarked_skills(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, ".agents/skills/unmarked/SKILL.md", _skill(""))

    assert check_skill_authors.main(["--repo-root", str(tmp_path)]) == 1
    assert ".agents/skills/unmarked/SKILL.md" in capsys.readouterr().err


def test_the_check_passes_when_every_skill_is_marked(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".agents/skills/marked/SKILL.md",
        _skill("metadata:\n  author: imbue\n"),
    )

    assert check_skill_authors.main(["--repo-root", str(tmp_path)]) == 0

from pathlib import Path

import pytest

from agents_shared import check_built_in_skills


def _skill(skills_dir: Path, name: str, metadata: str) -> None:
    (skills_dir / name).mkdir(parents=True)
    (skills_dir / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: x\n{metadata}---\n"
    )


def test_every_skill_without_the_author_marker_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _skill(tmp_path, "shipped", "metadata:\n  author: imbue\n")
    _skill(tmp_path, "someone-elses", "metadata:\n  author: someone-else\n")
    _skill(tmp_path, "unmarked", "")

    exit_code = check_built_in_skills.main(["--skills-dir", str(tmp_path)])

    assert exit_code == 1
    assert capsys.readouterr().err.splitlines()[1:] == [
        "  - someone-elses",
        "  - unmarked",
    ]

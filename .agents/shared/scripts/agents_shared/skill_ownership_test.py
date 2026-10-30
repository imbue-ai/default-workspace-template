from pathlib import Path

import pytest

from agents_shared import skill_ownership


def _skill(
    skills_dir: Path, name: str, metadata: str, *, script: str | None = None
) -> None:
    (skills_dir / name).mkdir(parents=True)
    (skills_dir / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: x\n{metadata}---\n"
    )
    if script is not None:
        (skills_dir / name / script).parent.mkdir(parents=True, exist_ok=True)
        (skills_dir / name / script).write_text("print('hi')\n")


def _run(
    skills_dir: Path, command: str, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    exit_code = skill_ownership.main(["--skills-dir", str(skills_dir), command])
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def test_unmarked_lists_only_skills_with_python_and_neither_marker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _skill(
        tmp_path, "built-in", "metadata:\n  author: imbue\n", script="scripts/run.py"
    )
    _skill(
        tmp_path,
        "made-here",
        "metadata:\n  crystallized: true\n",
        script="python/made_here_skill/run.py",
    )
    _skill(
        tmp_path,
        "installed",
        "metadata:\n  author: someone-else\n",
        script="scripts/tools/convert.py",
    )
    _skill(tmp_path, "prose-only", "")

    assert _run(tmp_path, "unmarked", capsys) == (0, "installed\n", "")


def test_check_built_in_takes_only_the_author_marker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _skill(tmp_path, "shipped", "metadata:\n  author: imbue\n")
    _skill(tmp_path, "made-here", "metadata:\n  crystallized: true\n")
    _skill(tmp_path, "unmarked", "")

    exit_code, _, err = _run(tmp_path, "check-built-in", capsys)

    assert exit_code == 1
    assert err.splitlines()[1:] == ["  - made-here", "  - unmarked"]

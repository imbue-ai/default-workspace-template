"""Tests for ``scaffold_skill``.

The uv steps go through an injected runner: ``uv lock`` and ``uv sync`` against a real
workspace would rewrite its lock, so they are recorded rather than run. The new entry
point's ``--help`` is run for real, with this interpreter, since it imports nothing the
venv has to install.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from agents_shared import scaffold_skill
from agents_shared.scaffold_skill import ScaffoldError, scaffold


class _Runner:
    """Records every command; runs an entry's ``--help`` for real, answers the rest."""

    def __init__(self, failing: Sequence[str] = ()) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self._failing = list(failing)

    def __call__(
        self, argv: Sequence[str], cwd: Path
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), cwd))
        if list(argv) == self._failing:
            return subprocess.CompletedProcess(argv, 1, "", "lock failed: no solution")
        if list(argv[:3]) == ["uv", "run", "--no-sync"]:
            return subprocess.run(
                [sys.executable, *argv[3:]],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
        return subprocess.CompletedProcess(argv, 0, "", "")


def _frontmatter(skill_md: Path) -> dict[str, object]:
    return yaml.safe_load(skill_md.read_text().split("---")[1])


def test_a_python_skill_is_installed_then_validated_with_a_runnable_entry(
    tmp_path: Path,
) -> None:
    runner = _Runner()
    description = 'Sync "Q4" orders: one row per order, then a summary'

    directory = scaffold(
        tmp_path,
        "order-sync",
        description,
        has_python=True,
        runner=runner,
    )

    entry = directory / "python" / "order_sync_skill" / "run.py"
    assert [argv for argv, _ in runner.calls] == [
        ["uv", "lock"],
        ["uv", "sync", "--all-packages"],
        ["uv", "lock", "--check"],
        ["uv", "run", "--no-sync", str(entry), "--help"],
    ]
    assert [cwd for _, cwd in runner.calls[:2]] == [tmp_path, tmp_path]
    assert _frontmatter(directory / "SKILL.md") == {
        "name": "order-sync",
        "description": description,
        "metadata": {"crystallized": True},
    }
    assert (
        "uv run --no-sync .agents/skills/order-sync/python/order_sync_skill/run.py"
        in (directory / "SKILL.md").read_text()
    )
    assert not (directory / "scripts").exists()


def test_a_prose_skill_is_only_its_skill_md(tmp_path: Path) -> None:
    runner = _Runner()

    directory = scaffold(
        tmp_path,
        "weekly-review",
        "Review the week",
        has_python=False,
        runner=runner,
    )

    assert sorted(path.name for path in directory.iterdir()) == ["SKILL.md"]
    assert runner.calls == []
    assert _frontmatter(directory / "SKILL.md")["metadata"] == {"crystallized": True}


def test_a_description_that_would_not_validate_leaves_nothing_behind(
    tmp_path: Path,
) -> None:
    runner = _Runner()

    with pytest.raises(ScaffoldError, match="description length"):
        scaffold(
            tmp_path,
            "too-long",
            "x" * 1025,
            has_python=True,
            runner=runner,
        )

    assert not scaffold_skill.skill_dir(tmp_path, "too-long").exists()
    assert runner.calls == []


@pytest.mark.parametrize("name", ["Order_Sync", "order--sync", "-order"])
def test_a_bad_name_writes_nothing(tmp_path: Path, name: str) -> None:
    with pytest.raises(ScaffoldError, match="is not a skill name"):
        scaffold(
            tmp_path,
            name,
            "Sync orders",
            has_python=True,
            runner=_Runner(),
        )

    assert not (tmp_path / ".agents").exists()


def test_an_existing_skill_is_left_untouched(tmp_path: Path) -> None:
    existing = scaffold_skill.skill_dir(tmp_path, "order-sync")
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("mine\n")

    with pytest.raises(ScaffoldError, match="already exists"):
        scaffold(
            tmp_path,
            "order-sync",
            "Sync orders",
            has_python=True,
            runner=_Runner(),
        )

    assert (existing / "SKILL.md").read_text() == "mine\n"


def test_a_failed_lock_is_reported_and_the_files_are_kept_to_fix(
    tmp_path: Path,
) -> None:
    runner = _Runner(failing=["uv", "lock"])

    with pytest.raises(ScaffoldError, match="no solution"):
        scaffold(
            tmp_path,
            "order-sync",
            "Sync orders",
            has_python=True,
            runner=runner,
        )

    assert scaffold_skill.entry_point(tmp_path, "order-sync").is_file()
    assert [argv for argv, _ in runner.calls] == [["uv", "lock"]]

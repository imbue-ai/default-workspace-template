"""Tests for ``resolve_template_base.py``, run against real git histories.

The base decides what a published template contains and what history it ships,
so these build the commit shapes bootstrap and update-self actually write and
assert on the content the resolved base carries.
"""

import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).with_name("resolve_template_base.py")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, relative: str, message: str) -> str:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{message}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _new_workspace(root: Path) -> tuple[Path, str, str]:
    """A template commit with bootstrap's marker on top; returns (repo, template, initial)."""
    repo = root / "workspace"
    repo.mkdir()
    _git(repo, "-c", "init.defaultBranch=main", "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "T")
    template = _commit(repo, "pyproject.toml", "Template release one")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "Initial workspace commit")
    return repo, template, _git(repo, "rev-parse", "HEAD")


def _resolve(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--repo", str(repo)],
        capture_output=True,
        text=True,
    )


def test_an_updated_workspace_resolves_to_the_upstream_template_it_merged(
    tmp_path: Path,
) -> None:
    repo, template, _ = _new_workspace(tmp_path)
    pre_update = _commit(repo, "system/apps/music_scout/main.py", "Build music scout")
    _git(repo, "checkout", "-q", "-b", "upstream", template)
    upstream = _commit(repo, "system/release_two.py", "Template release two")
    _git(repo, "checkout", "-q", "main")
    _git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "upstream",
        "-m",
        "update-self: merge upstream template (minds-v0.0.2)",
    )
    _commit(repo, "system/apps/demo/main.py", "Build the app being published")

    completed = _resolve(repo)

    assert completed.returncode == 0, completed.stderr
    base = completed.stdout.strip()
    assert base == upstream
    # What a publish on this base would ship: the new template release, and
    # neither the app built before the update nor the commit that built it.
    tree = _git(repo, "ls-tree", "-r", "--name-only", base).splitlines()
    assert "system/release_two.py" in tree
    assert "system/apps/music_scout/main.py" not in tree
    is_ancestor = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", pre_update, base]
    )
    assert is_ancestor.returncode == 1


def test_an_update_self_subject_that_merged_nothing_is_not_a_marker(
    tmp_path: Path,
) -> None:
    repo, _, initial = _new_workspace(tmp_path)
    _commit(repo, ".agents/skills/update-self/SKILL.md", "update-self: tidy the skill")

    completed = _resolve(repo)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == initial


def test_a_history_without_markers_exits_nonzero_for_the_callers_fallback(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "handmade"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "T")
    _commit(repo, "pyproject.toml", "Initial commit")

    completed = _resolve(repo)

    assert completed.returncode == 1
    assert completed.stdout == ""

"""Tests for the worker's CLAUDE.md override.

The override exists so a worker is not handed `AGENTS.md`, which is written for the agent that
holds the chat with the user. Its one dangerous property is that `CLAUDE.md` is tracked and the
worker commits its worktree for the orchestrator to merge -- so the test that matters is that the
override cannot reach a commit and cannot change what the build branch says.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "use_worker_instructions.sh"
_RULES = ".agents/skills/build-app/references/worker-workspace-rules.md"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo shaped like the worker's worktree: a tracked CLAUDE.md importing AGENTS.md."""
    repo = tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "CLAUDE.md").write_text("@AGENTS.md\n")
    (repo / "AGENTS.md").write_text("# for the chat agent\n")
    rules = repo / _RULES
    rules.parent.mkdir(parents=True)
    rules.write_text("# Workspace rules for a worker\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _run(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT)], cwd=repo, capture_output=True, text=True, check=True
    )


def test_claude_md_imports_the_worker_rules(repo: Path) -> None:
    _run(repo)
    assert (repo / "CLAUDE.md").read_text() == f"@{_RULES}\n"


def test_the_override_leaves_the_worktree_clean(repo: Path) -> None:
    """A dirty CLAUDE.md would show up in the worker's own `git status` and confuse it."""
    _run(repo)
    assert _git(repo, "status", "--porcelain") == ""


def test_the_override_never_reaches_a_commit(repo: Path) -> None:
    """The worker commits with `git add -A`; CLAUDE.md must not ride along."""
    _run(repo)
    (repo / "worker_output.txt").write_text("built something\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "worker work")
    committed = _git(repo, "show", "--stat", "--name-only", "--format=", "HEAD").split()
    assert committed == ["worker_output.txt"]
    assert _git(repo, "show", "HEAD:CLAUDE.md") == "@AGENTS.md\n"


def test_it_leaves_claude_md_alone_when_the_rules_are_missing(repo: Path) -> None:
    """A worktree without the skill is not one to silently point at a file that isn't there."""
    (repo / _RULES).unlink()
    _run(repo)
    assert (repo / "CLAUDE.md").read_text() == "@AGENTS.md\n"

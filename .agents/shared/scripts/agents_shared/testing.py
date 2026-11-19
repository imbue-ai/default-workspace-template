"""Test helpers shared by the suites of the scripts that boot isolated instances."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _hermetic_git_env() -> dict[str, str]:
    """The environment for a test's git: identity pinned and the user's git config
    disabled, so a developer's gpgsign or hooks cannot make it flaky."""
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Test Workspace",
        "GIT_AUTHOR_EMAIL": "workspace@example.invalid",
        "GIT_COMMITTER_NAME": "Test Workspace",
        "GIT_COMMITTER_EMAIL": "workspace@example.invalid",
    }


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        env=_hermetic_git_env(),
    )


def add_linked_worktree(live: Path, worktree: Path) -> None:
    """Commit everything under ``live`` as a new git checkout and add ``worktree`` as a linked worktree of it.

    ``data/`` is left out of the commit, as the workspace's ``.gitignore`` leaves it out,
    so the worktree holds the committed code and none of the live checkout's data.
    """
    live.mkdir(parents=True, exist_ok=True)
    (live / ".gitignore").write_text("data/\n")
    _git(live, "init", "-q", "-b", "main")
    _git(live, "add", "-A")
    _git(live, "commit", "-q", "-m", "workspace")
    _git(live, "worktree", "add", "-q", "-b", "mngr/change", str(worktree))

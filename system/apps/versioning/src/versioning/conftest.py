import subprocess
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest

# Every scratch repo commits under a fixed identity so tests are deterministic.
_GIT_ENV_ARGS = [
    "-c",
    "user.name=Chat-Test",
    "-c",
    "user.email=test@example.com",
    "-c",
    "commit.gpgsign=false",
]


def run_git(repo_root: Path, arguments: list[str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root)] + _GIT_ENV_ARGS + arguments,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, f"git {arguments} failed: {completed.stderr}"
    return completed.stdout


@pytest.fixture
def scratch_repo(tmp_path: Path) -> Path:
    """A fresh git repo with an empty initial commit, isolated per test."""
    repo_root = tmp_path / f"repo_{uuid4().hex}"
    repo_root.mkdir()
    run_git(repo_root, ["init", "-q", "-b", "main"])
    (repo_root / ".gitkeep").write_text("")
    run_git(repo_root, ["add", "-A"])
    run_git(repo_root, ["commit", "-q", "-m", "initial"])
    return repo_root


@pytest.fixture
def commit_app_file(scratch_repo: Path) -> Callable[[str, str, str, str], str]:
    """Write one file under an app folder and commit it with the given message; returns the sha."""

    def _commit(app_dir: str, file_name: str, content: str, message: str) -> str:
        target = scratch_repo / "system/apps" / app_dir / file_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        run_git(scratch_repo, ["add", "-A"])
        run_git(scratch_repo, ["commit", "-q", "-m", message])
        return run_git(scratch_repo, ["rev-parse", "HEAD"]).strip()

    return _commit

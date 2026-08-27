import subprocess
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class LocalGitRepo(FrozenModel):
    """A throwaway local git repo standing in for a remote (unit tests make no network requests)."""

    repo_dir: Path = Field(description="The repo's working directory, usable as a git remote url")
    commit_shas: tuple[str, ...] = Field(description="Every commit sha on 'main', oldest first")


def commit_readme_revision(repo_dir: Path, readme_content: str, message: str) -> str:
    """Rewrite README.md, commit it on the repo's current branch, and return the new commit's sha."""
    (repo_dir / "README.md").write_text(readme_content)
    subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True)
    # Identity and signing are set per invocation so the commit does not depend
    # on the developer's global git config (a global commit.gpgsign would try to
    # sign these throwaway commits and fail).
    subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "-c",
            "user.email=test@test",
            "-c",
            "user.name=test",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            message,
        ],
        check=True,
    )
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def make_local_git_repo(parent_dir: Path, repo_name: str, commit_count: int) -> LocalGitRepo:
    """Build a repo on branch 'main' whose every commit rewrites README.md with its own index, so a
    checkout's content identifies which commit it is at."""
    repo_dir = parent_dir / repo_name
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_dir)], check=True)
    commit_shas = [
        commit_readme_revision(
            repo_dir, "{} revision {}\n".format(repo_name, commit_idx), "commit {}".format(commit_idx)
        )
        for commit_idx in range(commit_count)
    ]
    return LocalGitRepo(repo_dir=repo_dir, commit_shas=tuple(commit_shas))

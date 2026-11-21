"""What the workspace repository's history says about one user app, and the
split that turns it into the app's own history.

A user app lives in the workspace repository at `system/apps/<package>` and has
a repository of its own, which `git subtree split` feeds: it turns the workspace
commits that touched the app's directory into commits of the app's own history,
and produces the same commits again from a longer history. `app-install` and the
sync pass both ask the questions here, so they give one answer.
"""

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from app_manifest.records import (
    AppOrigin,
    AppRecord,
    app_directory_prefix,
    join_marker_pattern,
    modifying_paths,
)
from loguru import logger

from github_sync.config import GithubSyncError

# A split walks every workspace commit made since the app's last join marker.
GIT_TIMEOUT_SECONDS = 600.0
_SUBTREE_COMMAND_FILENAME = "git-subtree"


class AppHistoryError(GithubSyncError):
    """A git command that reads or splits an app's history failed."""


class GitSubtreeMissingError(AppHistoryError):
    """The git installation has no `git subtree` command."""


def run_git(
    repo_root: Path,
    arguments: Sequence[str],
    environment: Mapping[str, str] | None = None,
) -> str:
    """Run one git command in `repo_root` with hooks off; its stdout, stripped.

    Raises AppHistoryError, carrying what git said, when the command fails.
    """
    command_environment = dict(os.environ if environment is None else environment)
    # Never wait on a prompt: nothing here runs with a person at a terminal.
    command_environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            ["git", "-c", f"core.hooksPath={os.devnull}", *arguments],
            cwd=repo_root,
            env=command_environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AppHistoryError(f"could not run `git {' '.join(arguments)}`: {e}") from e
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise AppHistoryError(f"`git {' '.join(arguments)}` failed: {detail}")
    return completed.stdout.strip()


def require_git_subtree(repo_root: Path) -> None:
    """Raise GitSubtreeMissingError, naming the command, when this git has no
    `git subtree`: some distributions package it apart from git itself."""
    exec_path = run_git(repo_root, ["--exec-path"])
    if not (Path(exec_path) / _SUBTREE_COMMAND_FILENAME).is_file():
        raise GitSubtreeMissingError(
            f"the `git subtree` command is not installed (there is no "
            f"{_SUBTREE_COMMAND_FILENAME} in {exec_path}), and an app's own "
            "repository is kept with it; install the package that carries it "
            "(`git` on Debian and Ubuntu, `git-subtree` on Fedora) and run this again"
        )


def is_primary_checkout(repo_root: Path) -> bool:
    """Whether `repo_root` is the root of a repository's primary checkout,
    rather than a directory inside one or a linked worktree, which is on a
    branch of its own."""
    top_level = Path(run_git(repo_root, ["rev-parse", "--show-toplevel"]))
    if top_level.resolve() != repo_root.resolve():
        return False
    git_dir = Path(run_git(repo_root, ["rev-parse", "--absolute-git-dir"]))
    common_dir = Path(
        run_git(repo_root, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    )
    return git_dir.resolve() == common_dir.resolve()


def app_tree(repo_root: Path, record: AppRecord) -> str | None:
    """The tree of the app's directory at the checked-out commit, or None when
    that commit does not hold the directory."""
    try:
        return run_git(
            repo_root,
            [
                "rev-parse",
                "--verify",
                "--quiet",
                f"HEAD:{app_directory_prefix(record)}",
            ],
        )
    except AppHistoryError as e:
        logger.debug("No committed directory for the app {}: {}", record.name, e)
        return None


def is_app_linked(repo_root: Path, record: AppRecord) -> bool:
    """Whether the history of the checked-out commit holds a join marker for the
    app's directory, so a split starts from it rather than walking the whole
    workspace history.

    `git subtree split --rejoin` leaves one, which is what linking an app this
    workspace built does, and so does every `git subtree add` and
    `git subtree merge` with `--squash`, so an installed app is linked from the
    start.
    """
    marker = run_git(
        repo_root,
        ["rev-list", "--max-count=1", f"--grep={join_marker_pattern(record)}", "HEAD"],
    )
    return bool(marker)


def is_app_modified(repo_root: Path, record: AppRecord) -> bool:
    """Whether an installed app's committed directory differs from what was
    installed in any path outside the regenerable set. An app this workspace
    built is never modified: it has nothing it was installed from."""
    if record.origin is not AppOrigin.INSTALLED or not record.upstream_tree:
        return False
    tree = app_tree(repo_root, record)
    if tree is None or tree == record.upstream_tree:
        return False
    try:
        differing = run_git(
            repo_root,
            ["diff", "--name-only", "--no-renames", record.upstream_tree, tree],
        )
    except AppHistoryError as e:
        # Without the installed tree the difference cannot be shown to be in
        # regenerable files alone.
        logger.warning(
            "Could not compare {} with the tree it was installed from, so it is "
            "taken as modified: {}",
            record.name,
            e,
        )
        return True
    return bool(modifying_paths(differing.splitlines()))


def split_app(repo_root: Path, record: AppRecord) -> str:
    """The commit of the app's own history that the checked-out commit splits
    to. Writes loose objects only: no ref moves and nothing is committed.

    Raises GitSubtreeMissingError when git has no `git subtree`, and
    AppHistoryError when the split fails (no commit holds the app's directory).
    """
    require_git_subtree(repo_root)
    output = run_git(
        repo_root,
        ["subtree", "split", "--quiet", f"--prefix={app_directory_prefix(record)}"],
    )
    lines = output.splitlines()
    if not lines:
        raise AppHistoryError(
            f"`git subtree split` of {app_directory_prefix(record)} named no commit"
        )
    return lines[-1].strip()

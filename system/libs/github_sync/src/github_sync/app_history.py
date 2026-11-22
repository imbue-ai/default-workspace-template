"""What the workspace repository's history says about one user app, and the
split that turns it into the app's own history.

A user app lives in the workspace repository at `system/apps/<package>` and has
a repository of its own, which `git subtree split` feeds: it turns the workspace
commits that touched the app's directory into commits of the app's own history,
and produces the same commits again from a longer history. `app-install` and the
sync pass both ask the questions here, so they give one answer.
"""

import os
import shutil
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
# Where `git subtree split` keeps its scratch files, under the git directory.
_SPLIT_CACHE_DIRNAME = "subtree-cache"


class AppHistoryError(GithubSyncError):
    """A git command that reads or splits an app's history failed."""


class GitSubtreeMissingError(AppHistoryError):
    """The git installation has no `git subtree` command."""


class WorkspaceHistoryInSplitError(AppHistoryError):
    """A split of an app's directory holds the workspace's own commits."""


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
    app's directory.

    `git subtree split --rejoin` leaves one, which is what linking an app this
    workspace built does: a split then starts from it rather than walking the
    whole workspace history. Every `git subtree add` and `git subtree merge`
    with `--squash` leaves a commit the same search finds, so an installed app
    counts as linked from the start, but that commit shortens no split: git
    subtree only remembers which upstream commit it stands for, and a split of
    an installed app reads the whole workspace history each time.
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


def _is_process_running(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It runs as another user.
        return True
    return True


def _remove_finished_split_caches(cache_root: Path) -> None:
    """Delete the scratch directory of every split that is no longer running.

    `git subtree split` keeps one per run, named by its process id and holding
    a file per commit it walked, and never deletes it. One whose process is
    still running is another split's, and is left alone.
    """
    if not cache_root.is_dir():
        return
    for entry in cache_root.iterdir():
        if not entry.name.isdigit() or _is_process_running(int(entry.name)):
            continue
        try:
            shutil.rmtree(entry)
        except OSError as e:
            logger.warning(
                "Could not remove the finished split's scratch {}: {}", entry, e
            )


def require_split_holds_only_the_app(
    repo_root: Path, record: AppRecord, split_commit: str
) -> None:
    """Raise WorkspaceHistoryInSplitError when the history of `split_commit`
    holds the workspace's own commits.

    `git subtree split` keeps a workspace commit as it is, rather than making
    the app's copy of it, when the commit has no app directory and the commit
    before it had one: the directory was removed and made again. Everything
    that commit follows comes with it, so the split would carry the whole
    workspace, every file of it, into a repository meant to hold one app.
    """
    workspace_root = run_git(
        repo_root, ["rev-list", "--first-parent", "--max-parents=0", "HEAD"]
    )
    # Nothing of the workspace's first commit is left once the split's history
    # is taken away only when that history holds it.
    outside_split = run_git(
        repo_root, ["rev-list", "--count", workspace_root, f"^{split_commit}"]
    )
    if outside_split == "0":
        raise WorkspaceHistoryInSplitError(
            f"{app_directory_prefix(record)} was removed and made again in the "
            "workspace's history, and a split of such a directory carries the "
            "workspace's own commits, with every file of the workspace, into the "
            f"app's history; nothing was done with it, and {record.name} cannot "
            "have a repository of its own while it lives in that directory"
        )


def split_app(repo_root: Path, record: AppRecord) -> str:
    """The commit of the app's own history that the checked-out commit splits
    to. Writes loose objects only: no ref moves and nothing is committed.

    Raises GitSubtreeMissingError when git has no `git subtree`,
    WorkspaceHistoryInSplitError when the split holds the workspace's own
    commits, and AppHistoryError when the split fails (no commit holds the
    app's directory).
    """
    require_git_subtree(repo_root)
    git_directory = Path(run_git(repo_root, ["rev-parse", "--absolute-git-dir"]))
    try:
        output = run_git(
            repo_root,
            ["subtree", "split", "--quiet", f"--prefix={app_directory_prefix(record)}"],
        )
    finally:
        _remove_finished_split_caches(git_directory / _SPLIT_CACHE_DIRNAME)
    lines = output.splitlines()
    if not lines:
        raise AppHistoryError(
            f"`git subtree split` of {app_directory_prefix(record)} named no commit"
        )
    split_commit = lines[-1].strip()
    require_split_holds_only_the_app(repo_root, record, split_commit)
    return split_commit

"""The git commands the installer runs, and the two ways it reaches a repository on GitHub."""

import os
import re
import subprocess
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger

from app_install.errors import GitCommandError
from app_install.errors import MissingCommandError
from app_install.errors import RepositoryUnreachableError
from app_manifest.records import AppRecord
from github_sync import app_history
from github_sync.wiring import git_environment_through_gateway

# The installer's own committer: its commits come from a command, not from any one chat's agent, and the
# analytics feed tells them from the user's own work by this name.
COMMITTER_NAME: Final[str] = "app-install"
COMMITTER_EMAIL: Final[str] = "app-install@workspace.invalid"
GIT_TIMEOUT_SECONDS: Final[float] = 300.0
_VERSION_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(r"refs/tags/(v(\d+)\.(\d+)\.(\d+))$")
DEFAULT_BRANCH: Final[str] = "main"
_TEMPLATE_VERSION_TAG_GLOB: Final[str] = "minds-v*"
# What git says when a repository needs a credential the workspace does not hold. GitHub answers a private
# repository as "not found" to a caller it does not know.
_ACCESS_FAILURE_MARKERS: Final[tuple[str, ...]] = (
    "Authentication failed",
    "could not read Username",
    "Repository not found",
    "returned error: 401",
    "returned error: 403",
    "returned error: 404",
)


def _environment(is_global_config_read: bool, is_gateway_used: bool) -> dict[str, str]:
    environment = dict(os.environ)
    # Never wait on a prompt: the installer asks nothing.
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_AUTHOR_NAME"] = COMMITTER_NAME
    environment["GIT_AUTHOR_EMAIL"] = COMMITTER_EMAIL
    environment["GIT_COMMITTER_NAME"] = COMMITTER_NAME
    environment["GIT_COMMITTER_EMAIL"] = COMMITTER_EMAIL
    if is_gateway_used:
        # A workspace with no gateway has only its global configuration to read the repository with.
        environment = git_environment_through_gateway(environment) or environment
    if not is_global_config_read:
        # With GitHub sync enabled the container's global configuration routes every github.com URL through the
        # latchkey gateway; leaving it out is what makes a fetch anonymous.
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
    return environment


def run_git(
    cwd: Path,
    arguments: Sequence[str],
    is_global_config_read: bool = True,
    is_gateway_used: bool = False,
    extra_environment: Mapping[str, str] | None = None,
) -> str:
    """Run one git command in ``cwd`` with hooks off and the installer's identity; its stdout, stripped.

    Hooks are off because GitHub sync's post-commit hook would push the branch of every repository the
    installer commits in, its scratch worktrees included. With ``is_gateway_used`` every github.com URL the
    command names goes through the latchkey gateway, when the workspace has one, and the command then reads the
    gateway's wiring from here alone, not from the global configuration. ``extra_environment`` is added to the
    command's environment (an index file of the command's own, say). Raises GitCommandError when the command
    fails.
    """
    argv = ["git", "-c", f"core.hooksPath={os.devnull}", *arguments]
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env={**_environment(is_global_config_read, is_gateway_used), **(extra_environment or {})},
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitCommandError(f"could not run `git {' '.join(arguments)}`: {e}") from e
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise GitCommandError(f"`git {' '.join(arguments)}` failed: {detail}")
    return completed.stdout.strip()


def _is_access_refusal(failure: GitCommandError) -> bool:
    return any(marker in str(failure) for marker in _ACCESS_FAILURE_MARKERS)


def _run_reaching_remote(cwd: Path, arguments: Sequence[str], url: str) -> str:
    """Run a command that reads ``url``, anonymously first and, when that is refused for want of a credential,
    with the workspace's GitHub access: through the latchkey gateway, which holds whatever permission the user
    has granted, whether or not GitHub sync has wired it into the container's global git configuration. A
    failure no credential would change (a ref the repository does not have) is not tried again. Raises
    RepositoryUnreachableError when the repository is not reached."""
    try:
        return run_git(cwd, arguments, is_global_config_read=False)
    except GitCommandError as anonymous_failure:
        if not _is_access_refusal(anonymous_failure):
            raise RepositoryUnreachableError(
                f"could not read {url}: {anonymous_failure}", is_access_needed=False
            ) from anonymous_failure
        try:
            return run_git(cwd, arguments, is_global_config_read=True, is_gateway_used=True)
        except GitCommandError as e:
            raise RepositoryUnreachableError(
                f"could not read {url}: {e} (without the workspace's GitHub access: {anonymous_failure})",
                is_access_needed=_is_access_refusal(e),
            ) from e


def resolve_ref(cwd: Path, url: str, requested_ref: str | None) -> str:
    """The ref to take from ``url``: the one asked for, else its highest ``v<major>.<minor>.<patch>`` tag, else
    its ``main`` branch."""
    if requested_ref:
        return requested_ref
    listing = _run_reaching_remote(cwd, ["ls-remote", "--tags", "--refs", "--", url], url)
    versions: list[tuple[tuple[int, int, int], str]] = []
    for line in listing.splitlines():
        match = _VERSION_TAG_PATTERN.search(line)
        if match is not None:
            versions.append(((int(match.group(2)), int(match.group(3)), int(match.group(4))), match.group(1)))
    return max(versions)[1] if versions else DEFAULT_BRANCH


def fetch_commit(cwd: Path, url: str, ref: str) -> str:
    """Fetch ``ref`` of ``url`` into the repository at ``cwd`` and return the commit it names."""
    # The URL is a link someone was sent and the ref may come with it: after "--" git reads neither as an option.
    _run_reaching_remote(cwd, ["fetch", "--no-tags", "--", url, ref], url)
    return run_git(cwd, ["rev-parse", "FETCH_HEAD^{commit}"])


def is_commit_present(repo_root: Path, commit: str) -> bool:
    """Whether the repository holds the commit's objects."""
    try:
        run_git(repo_root, ["cat-file", "-e", f"{commit}^{{commit}}"])
    except GitCommandError as e:
        logger.debug("{} is not in the repository at {}: {}", commit, repo_root, e)
        return False
    return True


def workspace_template_version(repo_root: Path) -> str:
    """The template version the workspace is on: the nearest ``minds-v`` release tag its history holds, which is
    how the update pass names a workspace's version too. Empty when the history holds no such tag."""
    try:
        return run_git(repo_root, ["describe", "--tags", "--abbrev=0", "--match", _TEMPLATE_VERSION_TAG_GLOB, "HEAD"])
    except GitCommandError as e:
        logger.debug("No template release tag is reachable from the checkout at {}: {}", repo_root, e)
        return ""


def _has_unconcluded_merge(repo_root: Path) -> bool:
    try:
        run_git(repo_root, ["rev-parse", "--quiet", "--verify", "MERGE_HEAD"])
    except GitCommandError as e:
        logger.debug("No merge is in progress in {}: {}", repo_root, e)
        return False
    return True


def land_commit(repo_root: Path, commit: str) -> None:
    """Bring the installer's commit onto the primary checkout: as a fast-forward, or as a merge when the primary
    branch moved while the installer worked. Raises GitCommandError, with the primary checkout as it was, when
    the two do not merge, and when the primary checkout is in the middle of a merge of its own."""
    # Checked first: git refuses to start a merge over an unconcluded one, and the abort below would then take
    # back the merge that was already there, with whatever had been resolved in it.
    if _has_unconcluded_merge(repo_root):
        raise GitCommandError(
            f"{commit} cannot be landed while the primary checkout is in the middle of a merge, which is left as "
            "it is: conclude or abort that merge, then run the command again"
        )
    try:
        run_git(repo_root, ["merge", "--ff-only", commit])
        return
    except GitCommandError as fast_forward_failure:
        logger.debug("Landing {} by a merge, since it does not fast-forward: {}", commit, fast_forward_failure)
    try:
        run_git(repo_root, ["merge", "--no-edit", commit])
    except GitCommandError as merge_failure:
        # A merge that stops on a conflict leaves the checkout half merged, with conflict markers in live files.
        try:
            run_git(repo_root, ["merge", "--abort"])
        except GitCommandError as abort_failure:
            # git has nothing to abort when it refused to start the merge, which changed nothing.
            logger.debug("No merge of {} to take back: {}", commit, abort_failure)
        raise GitCommandError(
            f"{commit} does not merge into the primary checkout, which is left as it was: {merge_failure}"
        ) from merge_failure


def require_git_subtree(cwd: Path) -> None:
    """Raise MissingCommandError, naming the command, when this git has no ``git subtree``: installing, updating,
    and linking an app all go through it, and without it git answers with its own usage text."""
    try:
        app_history.require_git_subtree(cwd)
    except app_history.GitSubtreeMissingError as e:
        raise MissingCommandError(str(e)) from e
    except app_history.AppHistoryError as e:
        raise GitCommandError(str(e)) from e


def is_primary_checkout(repo_root: Path) -> bool:
    """Whether ``repo_root`` is the root of the repository's primary checkout, not a linked worktree."""
    try:
        return app_history.is_primary_checkout(repo_root)
    except app_history.AppHistoryError as e:
        raise GitCommandError(str(e)) from e


def is_app_linked(repo_root: Path, record: AppRecord) -> bool:
    """Whether the checked-out history holds a join marker for the app's directory, the answer the sync pass
    reads too."""
    try:
        return app_history.is_app_linked(repo_root, record)
    except app_history.AppHistoryError as e:
        raise GitCommandError(str(e)) from e


def is_app_modified(repo_root: Path, record: AppRecord) -> bool:
    """Whether an installed app's committed directory differs from what was installed in a path outside the
    regenerable set, the answer the sync pass reads too."""
    try:
        return app_history.is_app_modified(repo_root, record)
    except app_history.AppHistoryError as e:
        raise GitCommandError(str(e)) from e

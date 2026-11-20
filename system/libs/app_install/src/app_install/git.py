"""The git commands the installer runs, and the two ways it reaches a repository on GitHub."""

import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger

from app_install.errors import GitCommandError
from app_install.errors import RepositoryUnreachableError
from github_sync.wiring import gateway_git_config

# The installer's own committer: its commits come from a command, not from any one chat's agent, and the
# analytics feed tells them from the user's own work by this name.
COMMITTER_NAME: Final[str] = "app-install"
COMMITTER_EMAIL: Final[str] = "app-install@workspace.invalid"
GIT_TIMEOUT_SECONDS: Final[float] = 300.0
_VERSION_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(r"refs/tags/(v(\d+)\.(\d+)\.(\d+))$")
DEFAULT_BRANCH: Final[str] = "main"
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
    if not is_global_config_read:
        # With GitHub sync enabled the container's global configuration routes every github.com URL through the
        # latchkey gateway; leaving it out is what makes a fetch anonymous.
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
    if is_gateway_used:
        # In the environment and not in the arguments, so the gateway's password is in no process listing.
        first_index = int(environment.get("GIT_CONFIG_COUNT", "0"))
        entries = gateway_git_config() or []
        for index, (key, value) in enumerate(entries, start=first_index):
            environment[f"GIT_CONFIG_KEY_{index}"] = key
            environment[f"GIT_CONFIG_VALUE_{index}"] = value
        environment["GIT_CONFIG_COUNT"] = str(first_index + len(entries))
    return environment


def run_git(
    cwd: Path, arguments: Sequence[str], is_global_config_read: bool = True, is_gateway_used: bool = False
) -> str:
    """Run one git command in ``cwd`` with hooks off and the installer's identity; its stdout, stripped.

    Hooks are off because GitHub sync's post-commit hook would push the branch of every repository the
    installer commits in, its scratch worktrees included. With ``is_gateway_used`` every github.com URL the
    command names goes through the latchkey gateway, when the workspace has one. Raises GitCommandError when
    the command fails.
    """
    argv = ["git", "-c", f"core.hooksPath={os.devnull}", *arguments]
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=_environment(is_global_config_read, is_gateway_used),
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


def _run_reaching_remote(cwd: Path, arguments: Sequence[str], url: str) -> str:
    """Run a command that reads ``url``, anonymously first and then with the workspace's GitHub access: through
    the latchkey gateway, which holds whatever permission the user has granted, whether or not GitHub sync has
    wired it into the container's global git configuration. Raises RepositoryUnreachableError when neither
    reaches it."""
    try:
        return run_git(cwd, arguments, is_global_config_read=False)
    except GitCommandError as anonymous_failure:
        try:
            return run_git(cwd, arguments, is_global_config_read=True, is_gateway_used=True)
        except GitCommandError as e:
            is_access_needed = any(marker in str(e) for marker in _ACCESS_FAILURE_MARKERS)
            raise RepositoryUnreachableError(
                f"could not read {url}: {e} (without the workspace's GitHub access: {anonymous_failure})",
                is_access_needed=is_access_needed,
            ) from e


def resolve_ref(cwd: Path, url: str, requested_ref: str | None) -> str:
    """The ref to take from ``url``: the one asked for, else its highest ``v<major>.<minor>.<patch>`` tag, else
    its ``main`` branch."""
    if requested_ref:
        return requested_ref
    listing = _run_reaching_remote(cwd, ["ls-remote", "--tags", "--refs", url], url)
    versions: list[tuple[tuple[int, int, int], str]] = []
    for line in listing.splitlines():
        match = _VERSION_TAG_PATTERN.search(line)
        if match is not None:
            versions.append(((int(match.group(2)), int(match.group(3)), int(match.group(4))), match.group(1)))
    return max(versions)[1] if versions else DEFAULT_BRANCH


def fetch_commit(cwd: Path, url: str, ref: str) -> str:
    """Fetch ``ref`` of ``url`` into the repository at ``cwd`` and return the commit it names."""
    _run_reaching_remote(cwd, ["fetch", "--no-tags", url, ref], url)
    return run_git(cwd, ["rev-parse", "FETCH_HEAD^{commit}"])


def land_commit(repo_root: Path, commit: str) -> None:
    """Bring the installer's commit onto the primary checkout: as a fast-forward, or as a merge when the primary
    branch moved while the installer worked."""
    try:
        run_git(repo_root, ["merge", "--ff-only", commit])
    except GitCommandError as fast_forward_failure:
        logger.debug("Landing {} by a merge, since it does not fast-forward: {}", commit, fast_forward_failure)
        run_git(repo_root, ["merge", "--no-edit", commit])

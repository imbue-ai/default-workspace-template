"""The version of the workspace's own checkout, for the workspace routes' status answer.

What in the checkout's git names its version, and which of two names wins, is ``workspace_update_control``'s
(``installed_version``), which the update dispatcher reads the version by as well. This module runs the two git
commands the shell's way. Nothing is fetched: a checkout that names no version answers ''.
"""

from pathlib import Path
from typing import Final

from loguru import logger
from workspace_update_control.installed_version import DESCRIBE_COMMAND
from workspace_update_control.installed_version import UPDATE_SELF_MARKER_COMMAND
from workspace_update_control.installed_version import resolve_template_ref

from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version

_GIT_TIMEOUT_SECONDS: Final[float] = 10.0
_GIT_SHUTDOWN_TIMEOUT_SECONDS: Final[float] = 2.0
_LOGGED_STDERR_MAX_CHARACTERS: Final[int] = 300


def _git_output(command: tuple[str, ...], repo_root: Path, is_failure_ordinary: bool) -> str:
    """A read-only git command's output; '' when it could not run or exited with an error.

    ``is_failure_ordinary`` says an error exit is an ordinary state of a checkout, which is logged quietly; any
    other is a fault that would otherwise read as a checkout that names no version.
    """
    try:
        result = run_local_command_modern_version(
            command=list(command),
            cwd=repo_root,
            is_checked=False,
            timeout=_GIT_TIMEOUT_SECONDS,
            shutdown_timeout_sec=_GIT_SHUTDOWN_TIMEOUT_SECONDS,
        )
    except (ProcessError, OSError) as e:
        logger.warning("Could not run `{}` in {} for the workspace's version: {}", " ".join(command), repo_root, e)
        return ""
    if result.returncode != 0:
        logger.log(
            "DEBUG" if is_failure_ordinary else "WARNING",
            "`{}` exited {} in {} for the workspace's version: {}",
            " ".join(command),
            result.returncode,
            repo_root,
            result.stderr.strip()[-_LOGGED_STDERR_MAX_CHARACTERS:],
        )
        return ""
    return result.stdout


def read_template_ref(repo_root: Path) -> str:
    """The version of the checkout at ``repo_root``; '' when it names none."""
    return resolve_template_ref(
        # ``git log`` exits clean when no commit matches, so an error exit is never a checkout with no marker.
        _git_output(UPDATE_SELF_MARKER_COMMAND, repo_root, is_failure_ordinary=False),
        # ``git describe`` exits with an error for a checkout with no tag to describe, which a workspace created
        # from a published template is.
        _git_output(DESCRIBE_COMMAND, repo_root, is_failure_ordinary=True),
    )

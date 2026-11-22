"""One-way markers recording what a workspace's owner has already done.

The chat's activity suggestions offer things the owner has not tried yet. What tells them a thing
was tried may not last (a second chat is deleted, a phone is put away), so each fact is kept as a
marker that is only ever set: an empty file under the marker directory::

    data/.state/onboarding_activity/<marker>

A marker's modification time is when it was last set; setting it again refreshes that time and
changes nothing else. Nothing here removes a marker.

The marker directory is ``data/.state/onboarding_activity`` under the workspace's main checkout:
the repo the caller names (by default the one this package is in), or, when that is a git worktree
(a worker's work dir), the checkout the worktree belongs to, whose ``data/`` is the one the apps
read. ``$ONBOARDING_ACTIVITY_DIR`` overrides it.

A reader must be able to tell a marker that is not set from one that cannot be read, since the two
call for opposite answers (offer the suggestion, or offer nothing). ``is_marked`` and ``marked_at``
answer False and None only for a marker that is not there, and raise ``MarkerUnreadableError`` for
any other failure to look. ``mark`` raises ``MarkerNotWrittenError`` when the file cannot be made.
"""

import datetime
import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

MARKER_DIR_ENV = "ONBOARDING_ACTIVITY_DIR"
DEFAULT_MARKER_DIR = Path("data") / ".state" / "onboarding_activity"


class Marker(StrEnum):
    """Every fact a marker records; the value is the marker's file name."""

    # The shell's show-around tour has nothing left to show.
    TOUR_FINISHED = "tour_finished"
    # The owner has opened the workspace in the shell's phone layout.
    PHONE_LAYOUT_USED = "phone_layout_used"
    # A person started a chat while another chat a person talks to already existed.
    SECOND_CHAT_STARTED = "second_chat_started"
    # An agent filed a permission request in a chat the chat app follows.
    PERMISSION_REQUEST_FILED = "permission_request_filed"
    # The owner was shown where the workspace's connections live.
    CONNECT_DATA_COACH_SHOWN = "connect_data_coach_shown"
    # The build-app skill's scaffolder ran; its time is that of the latest run.
    APP_BUILD_STARTED = "app_build_started"


class OnboardingActivityError(Exception):
    """The marker directory could not be used."""


class MarkerUnreadableError(OnboardingActivityError):
    """Whether a marker is set could not be read, which is not the same as it not being set."""


class MarkerNotWrittenError(OnboardingActivityError):
    """A marker could not be set."""


def own_repo_root() -> Path:
    """The repo this package is in: the nearest ancestor holding ``system/scripts``, else cwd."""
    for ancestor in Path(__file__).resolve().parents:
        if (ancestor / "system" / "scripts").is_dir():
            return ancestor
    return Path.cwd()


def main_checkout(repo_root: Path) -> Path:
    """The checkout a git worktree belongs to, read from its ``.git`` file; ``repo_root`` itself
    when it is a main checkout or not a git checkout at all."""
    try:
        git_file = (repo_root / ".git").read_text(encoding="utf-8")
    except OSError:
        # No .git, or a directory: a main checkout or none.
        return repo_root
    git_dir_text = git_file.strip().removeprefix("gitdir:").strip()
    git_dir = (repo_root / git_dir_text).resolve()
    try:
        common_dir = (
            git_dir / (git_dir / "commondir").read_text(encoding="utf-8").strip()
        ).resolve()
    except OSError:
        return repo_root
    return common_dir.parent


def marker_dir(environ: Mapping[str, str], repo_root: Path | None = None) -> Path:
    """The marker directory of the checkout at ``repo_root`` (by default the one this package is in)."""
    override = environ.get(MARKER_DIR_ENV, "")
    if override:
        return Path(override)
    return (
        main_checkout(own_repo_root() if repo_root is None else repo_root)
        / DEFAULT_MARKER_DIR
    )


def _marker_path(marker: Marker, directory: Path | None) -> Path:
    return (marker_dir(os.environ) if directory is None else directory) / marker.value


def mark(marker: Marker, directory: Path | None = None) -> None:
    """Set the marker, or refresh its time when it is already set; raises MarkerNotWrittenError.

    ``directory`` is the marker directory, by default the workspace's own."""
    path = _marker_path(marker, directory)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError as error:
        raise MarkerNotWrittenError(
            f"Could not set the marker at {path}: {error}"
        ) from error


def marked_at(
    marker: Marker, directory: Path | None = None
) -> datetime.datetime | None:
    """When the marker was last set, in UTC; None when it is not set. Raises MarkerUnreadableError
    when that could not be read (anything but the file, or its directory, not being there)."""
    path = _marker_path(marker, directory)
    try:
        modified = path.stat().st_mtime
    except FileNotFoundError:
        return None
    except OSError as error:
        raise MarkerUnreadableError(
            f"Could not read the marker at {path}: {error}"
        ) from error
    return datetime.datetime.fromtimestamp(modified, datetime.timezone.utc)


def is_marked(marker: Marker, directory: Path | None = None) -> bool:
    """Whether the marker is set; raises MarkerUnreadableError when that could not be read."""
    return marked_at(marker, directory) is not None

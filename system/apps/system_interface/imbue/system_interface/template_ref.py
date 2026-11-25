"""The version of the workspace's own checkout, for the workspace routes' status answer.

Two things in the checkout's git can name it: the subject the update-self skill writes when a run lands
(``update-self: merge upstream template (<ref>)``) and the nearest reachable ``minds-v*`` tag. The merge's ref is
taken unless the tag is a newer release: a workspace updated to ``main`` still describes the release it was created
at, while a run landed with an ordinary merge leaves its marker off the first-parent line, where only the tag
reaches the release it brought in.

This is how the Imbue Studio desktop reads the version too (``workspace_version.py`` in the mngr repository), and
the two must agree. Nothing is fetched: a checkout that names no version answers ''.
"""

import re
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.pure import pure

_UPDATE_SELF_SUBJECT_PREFIX: Final[str] = "update-self: merge upstream template ("
_UPDATE_SELF_MARKER_COMMAND: Final[tuple[str, ...]] = (
    "git",
    "log",
    "--first-parent",
    f"--grep=^{_UPDATE_SELF_SUBJECT_PREFIX}",
    "-1",
    "--format=%s",
)
_DESCRIBE_COMMAND: Final[tuple[str, ...]] = ("git", "describe", "--tags", "--match", "minds-v*", "--abbrev=0")
# Anchored on the subject: ``--grep`` also matches a body that merely quotes the marker.
_UPDATE_SELF_SUBJECT_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"^{re.escape(_UPDATE_SELF_SUBJECT_PREFIX)}([^()]+)\)\s*$"
)
# The grammar of a release tag, as the update-self skill's ``update_target.py`` reads it.
_RELEASE_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(r"^minds-v(\d+)\.(\d+)\.(\d+)(?:-(?P<pre>.+))?$")
_GIT_TIMEOUT_SECONDS: Final[float] = 10.0
_GIT_SHUTDOWN_TIMEOUT_SECONDS: Final[float] = 2.0

_ReleaseSortKey = tuple[int, int, int, int, tuple[tuple[int, int, str], ...]]


@pure
def _release_sort_key(ref: str) -> _ReleaseSortKey | None:
    """How a ``minds-v*`` tag orders among releases, the way semver orders; None for any other ref."""
    match = _RELEASE_TAG_PATTERN.match(ref.strip())
    if match is None:
        return None
    prerelease = match.group("pre")
    # A numeric identifier compares numerically and ranks below an alphanumeric one.
    identifiers = tuple(
        (0, int(identifier), "") if identifier.isdecimal() else (1, 0, identifier)
        for identifier in (prerelease.split(".") if prerelease is not None else ())
    )
    release_rank = 0 if prerelease is not None else 1
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), release_rank, identifiers)


@pure
def resolve_template_ref(marker_subject: str, described_tag: str) -> str:
    """The version the marker's subject and the described tag name between them; '' when neither names one."""
    match = _UPDATE_SELF_SUBJECT_PATTERN.match(marker_subject.strip())
    marker_ref = match.group(1).strip() if match is not None else ""
    tag_ref = described_tag.strip()
    if not marker_ref:
        return tag_ref
    marker_key = _release_sort_key(marker_ref)
    tag_key = _release_sort_key(tag_ref)
    if marker_key is not None and tag_key is not None and marker_key < tag_key:
        return tag_ref
    return marker_ref


def _git_output(command: tuple[str, ...], repo_root: Path) -> str:
    """A read-only git command's output; '' when it could not run or found nothing (a checkout with no tag)."""
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
    return result.stdout if result.returncode == 0 else ""


def read_template_ref(repo_root: Path) -> str:
    """The version of the checkout at ``repo_root``; '' when it names none."""
    return resolve_template_ref(
        _git_output(_UPDATE_SELF_MARKER_COMMAND, repo_root), _git_output(_DESCRIBE_COMMAND, repo_root)
    )

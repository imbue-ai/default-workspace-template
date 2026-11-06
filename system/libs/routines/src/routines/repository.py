import subprocess
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from routines.errors import DefinitionCommitError
from routines.interfaces import ClockInterface
from routines.interfaces import RepositoryInterface

COMMIT_TIMEOUT_SECONDS: Final[float] = 30.0
_RETRY_PAUSE_SECONDS: Final[float] = 1.0
# The library's own committer: a definition write comes from a click or a command, not from any one chat's agent.
COMMITTER_NAME: Final[str] = "Routines"
COMMITTER_EMAIL: Final[str] = "routines@workspace.invalid"
# What `git add` says of a path that neither the working tree nor the index has.
_UNKNOWN_PATH_MARKER: Final[str] = "did not match any files"
COULD_NOT_SAVE_MESSAGE: Final[str] = (
    "Couldn't save that change because something else is saving right now. Try again in a moment."
)
# What git says when the index is locked by another writer or a merge is in progress: the transient cases the
# message above is for; anything else is reported with git's own words.
_ANOTHER_WRITER_MARKERS: Final[tuple[str, ...]] = ("index.lock", "MERGE_HEAD", "merge in progress", "merging")


def _is_another_writer(failure: str) -> bool:
    return any(marker in failure for marker in _ANOTHER_WRITER_MARKERS)


class GitRepository(RepositoryInterface):
    """Commits through the git command, one path at a time, retrying once past another writer."""

    clock: ClockInterface = Field(frozen=True, description="For the pause before the retry")

    def commit_path(self, path: Path, message: str) -> None:
        relative = path.resolve().relative_to(self.repo_root.resolve())
        first = self._try_commit(relative, message)
        if first is None:
            return
        if not _is_another_writer(first):
            logger.warning("Could not commit {}: {}", relative, first)
            raise DefinitionCommitError(f"Couldn't save that change: {first}")
        logger.debug("Retrying the commit of {} once past another writer: {}", relative, first)
        self.clock.pause(_RETRY_PAUSE_SECONDS)
        second = self._try_commit(relative, message)
        if second is None:
            return
        logger.warning("Could not commit {}: {}", relative, second)
        if _is_another_writer(second):
            raise DefinitionCommitError(COULD_NOT_SAVE_MESSAGE)
        raise DefinitionCommitError(f"Couldn't save that change: {second}")

    def _try_commit(self, relative: Path, message: str) -> str | None:
        """The failure's text, or None once the path is committed (or has nothing to commit)."""
        try:
            staged = subprocess.run(
                ["git", "add", "--", str(relative)],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=COMMIT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return str(e)
        if staged.returncode != 0:
            detail = (staged.stderr or staged.stdout).strip()
            if _UNKNOWN_PATH_MARKER in detail:
                # A deleted file git never tracked (a definition written by hand and never committed): nothing
                # to record.
                return None
            return detail
        argv = [
            "git",
            "-c",
            f"user.name={COMMITTER_NAME}",
            "-c",
            f"user.email={COMMITTER_EMAIL}",
            "commit",
            "--quiet",
            "--only",
            "-m",
            message,
            "--",
            str(relative),
        ]
        try:
            completed = subprocess.run(
                argv,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=COMMIT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return str(e)
        if completed.returncode == 0:
            return None
        detail = (completed.stderr or completed.stdout).strip()
        if "nothing to commit" in detail or "no changes added to commit" in detail:
            return None
        return detail

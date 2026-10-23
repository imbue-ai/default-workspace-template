import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from skills_catalog.data_types import SkillsHistory
from skills_catalog.errors import GitHistoryError
from skills_catalog.interfaces import SkillHistoryReaderInterface
from skills_catalog.primitives import SKILLS_DIR
from skills_catalog.provenance import LOG_FORMAT
from skills_catalog.provenance import history_from_git_log
from skills_catalog.provenance import skill_names_with_unsaved_changes

GIT_TIMEOUT_SECONDS: Final[float] = 30.0
# A git read slower than this still answers, but is logged so a slowing checkout is noticed.
GIT_SLOW_WARNING_SECONDS: Final[float] = 5.0


class GitSkillHistoryReader(SkillHistoryReaderInterface):
    """Reads the skills directory's history with the git command, once per HEAD."""

    repo_root: Path = Field(frozen=True, description="The workspace checkout")
    # The HEAD the history was read at, with that history: one pair, replaced whole, since requests run on threads.
    _cached: tuple[str, SkillsHistory] | None = PrivateAttr(default=None)

    def read_history(self) -> SkillsHistory:
        head = self._run_git(("rev-parse", "HEAD")).strip()
        cached = self._cached
        if cached is not None and cached[0] == head:
            return cached[1]
        log_output = self._run_git(("log", f"--format={LOG_FORMAT}", "--name-only", head, "--", SKILLS_DIR.as_posix()))
        history = history_from_git_log(log_output)
        self._cached = (head, history)
        return history

    def read_names_with_unsaved_changes(self) -> frozenset[str]:
        status_output = self._run_git(
            ("status", "--porcelain", "-z", "--untracked-files=all", "--", SKILLS_DIR.as_posix())
        )
        return skill_names_with_unsaved_changes(status_output)

    def _run_git(self, arguments: Sequence[str]) -> str:
        started_at = time.monotonic()
        try:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise GitHistoryError(f"git {arguments[0]} did not run in {self.repo_root}: {e}") from e
        elapsed = time.monotonic() - started_at
        if elapsed > GIT_SLOW_WARNING_SECONDS:
            logger.warning("Ran git {} in {} slowly, in {:.1f}s", arguments[0], self.repo_root, elapsed)
        if completed.returncode != 0:
            detail = completed.stderr.strip()[-300:]
            raise GitHistoryError(f"git {arguments[0]} in {self.repo_root} exited {completed.returncode}: {detail}")
        return completed.stdout

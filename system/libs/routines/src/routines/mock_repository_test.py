from pathlib import Path

from pydantic import Field

from routines.errors import DefinitionCommitError
from routines.interfaces import RepositoryInterface


class RecordingRepository(RepositoryInterface):
    """Records what it was asked to commit; refuses when told to."""

    commits: list[tuple[Path, str]] = Field(default_factory=list, description="Every commit asked for")
    is_refusing: bool = Field(default=False, description="Whether every commit fails")

    def commit_path(self, path: Path, message: str) -> None:
        if self.is_refusing:
            raise DefinitionCommitError("Couldn't save that change because something else is saving right now.")
        self.commits.append((path, message))

from abc import ABC
from abc import abstractmethod
from pathlib import Path

from imbue.imbue_common.mutable_model import MutableModel
from pydantic import Field

from versioning.data_types import ChangeStats
from versioning.data_types import CommitRecord
from versioning.data_types import FileChange


class GitRepoInterface(MutableModel, ABC):
    """Contract for reading and writing the shared workspace git repo."""

    repo_root: Path = Field(frozen=True, description="Working tree root of the repo")

    @abstractmethod
    def read_commits_touching_path(self, relative_path: str) -> list[CommitRecord]:
        """Return every commit that touched the path, oldest first."""

    @abstractmethod
    def read_change_stats_by_sha(self, relative_path: str) -> dict[str, ChangeStats]:
        """Return per-commit change sizes under the path, keyed by full sha."""

    @abstractmethod
    def read_diff_stat_between(self, sha: str, relative_path: str) -> str:
        """Return `git diff --stat` output between the sha and the working tree, for the path."""

    @abstractmethod
    def read_changed_file_count_between(self, sha: str, relative_path: str) -> int:
        """Return how many files under the path differ between the sha and the working tree."""

    @abstractmethod
    def read_diff_of_commits(self, shas: list[str], relative_path: str) -> str:
        """Return the combined patch the shas apply to the path (first sha's parent to last sha)."""

    @abstractmethod
    def read_file_changes_of_commit(self, sha: str, relative_path: str) -> list[FileChange]:
        """Return what each file under the path did in the commit (added/edited/removed)."""

    @abstractmethod
    def read_dirty_paths_under(self, relative_path: str) -> list[str]:
        """Return repo-relative paths under the given path with uncommitted changes."""

    @abstractmethod
    def commit_paths(self, relative_path: str, message: str) -> str:
        """Stage everything under the path and commit it, returning the new commit sha."""

    @abstractmethod
    def restore_path_to_commit(self, sha: str, relative_path: str) -> None:
        """Make the working tree under the path match the given commit exactly (staged, uncommitted)."""

    @abstractmethod
    def read_file_at_commit(self, sha: str, repo_relative_file: str) -> str | None:
        """Return the file's content at the commit, or None if it did not exist there."""

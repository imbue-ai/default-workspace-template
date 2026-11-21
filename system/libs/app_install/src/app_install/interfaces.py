from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from pathlib import Path

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from pydantic import Field

from app_manifest.manifest import EnvironmentNote


class CreatedRepository(FrozenModel):
    """A GitHub repository the runtime created."""

    url: str = Field(description="The repository's https://github.com/<owner>/<repo> URL")
    lockdown_problem: str = Field(
        default="", description="Why closing the repository to outside contributions did not take, if it did not"
    )


class WorkspaceRuntimeInterface(MutableModel, ABC):
    """The running workspace around the repository, as far as the installer touches it: supervisord, the app
    registry, the repository's history, and the workspace's GitHub account."""

    repo_root: Path = Field(frozen=True, description="The workspace repo's root")

    @abstractmethod
    def reload_programs(self) -> str | None:
        """Have supervisord pick up the program files as they are now, stopping the programs whose file is gone
        and starting new ones; the reason when it could not, None when it did."""

    @abstractmethod
    def wait_for_port(self, port: int) -> bool:
        """Whether something answered on the loopback port before the runtime's patience ran out."""

    @abstractmethod
    def install_environment(self, environment: EnvironmentNote) -> tuple[str, ...]:
        """Install the system packages and global tools an app declares; the ones that could not be installed,
        each with the reason."""

    @abstractmethod
    def restart_programs(self, programs: Sequence[str]) -> str | None:
        """Restart the named supervisord programs; the reason when it could not, None when it did."""

    @abstractmethod
    def relock_app(self, worktree: Path, package: str) -> str | None:
        """Make the lock of the Python app at ``system/apps/<package>`` of ``worktree`` again from its project;
        the reason when it could not, None when it did or the app has no project."""

    @abstractmethod
    def verify_app(self, worktree: Path, package: str) -> str | None:
        """Run the checks of the app at ``system/apps/<package>`` of ``worktree`` (its environment syncs from its
        lock and its tests pass); why they fail, None when they pass or the app has none."""

    @abstractmethod
    def free_disk_bytes(self, directory: Path) -> int:
        """How many bytes the disk holding ``directory`` can still take."""

    @abstractmethod
    def has_recent_backup(self) -> bool:
        """Whether the workspace's backup holds a recent restore point."""

    @abstractmethod
    def remove_registry_row(self, name: str) -> str | None:
        """Remove the app's row from the app registry; the reason when it could not, None when it did."""

    @abstractmethod
    def commit_paths(self, relative_paths: Sequence[str], message: str) -> str | None:
        """Commit the working-tree state of exactly these paths under the installer's own author; the reason when
        it could not, None when it did or when there was nothing to commit."""

    @abstractmethod
    def delete_ref(self, ref: str) -> str | None:
        """Delete a local ref of the repository if it exists; the reason when it could not, None when the ref is
        gone."""

    @abstractmethod
    def create_private_repository(self, name: str, description: str) -> CreatedRepository:
        """Create the private repository ``name`` under the workspace's GitHub account and close it to outside
        contributions. Raises DevRepositoryError when it is not created; a name the account already uses is one
        such case, and the message says so."""

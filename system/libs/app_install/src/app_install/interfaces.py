from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from pathlib import Path

from imbue.imbue_common.mutable_model import MutableModel
from pydantic import Field

from app_manifest.manifest import EnvironmentNote


class WorkspaceRuntimeInterface(MutableModel, ABC):
    """The running workspace around the repository, as far as the installer touches it: supervisord, the app
    registry, and the repository's history."""

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
    def remove_registry_row(self, name: str) -> str | None:
        """Remove the app's row from the app registry; the reason when it could not, None when it did."""

    @abstractmethod
    def commit_paths(self, relative_paths: Sequence[str], message: str) -> str | None:
        """Commit the working-tree state of exactly these paths under the installer's own author; the reason when
        it could not, None when it did or when there was nothing to commit."""

    @abstractmethod
    def delete_ref(self, ref: str) -> None:
        """Delete a local ref of the repository if it exists."""

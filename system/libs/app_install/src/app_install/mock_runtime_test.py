from collections.abc import Sequence
from pathlib import Path

from pydantic import Field

from app_install.interfaces import WorkspaceRuntimeInterface
from app_manifest.manifest import EnvironmentNote


class RecordingRuntime(WorkspaceRuntimeInterface):
    """A workspace runtime that does nothing outside the tree and records what it was asked to do."""

    calls: list[str] = Field(default_factory=list, description="Every call, in order, as one line each")
    reload_failure: str | None = Field(default=None, description="What reloading programs fails with, if it does")
    registry_failure: str | None = Field(default=None, description="What removing a row fails with, if it does")
    commit_failure: str | None = Field(default=None, description="What a commit fails with, if it does")
    is_port_answering: bool = Field(default=True, description="Whether a started app answers on its port")
    relocked_lock_text: str = Field(default="relocked\n", description="What a relock writes as the app's lock")
    verify_failure: str | None = Field(default=None, description="Why the app's checks fail, if they do")
    is_backup_recent: bool = Field(default=False, description="Whether the workspace has a recent backup")
    free_bytes: int = Field(default=10**12, description="How much room the disk has")
    unresolved_environment: tuple[str, ...] = Field(default=(), description="What the environment install fails on")

    def reload_programs(self) -> str | None:
        self.calls.append("reload_programs")
        return self.reload_failure

    def wait_for_port(self, port: int) -> bool:
        self.calls.append(f"wait_for_port {port}")
        return self.is_port_answering

    def install_environment(self, environment: EnvironmentNote) -> tuple[str, ...]:
        self.calls.append(f"install_environment apt={list(environment.apt)}")
        return self.unresolved_environment

    def restart_programs(self, programs: Sequence[str]) -> str | None:
        self.calls.append(f"restart_programs {list(programs)}")
        return None

    def relock_app(self, worktree: Path, package: str) -> str | None:
        self.calls.append(f"relock_app {package}")
        (worktree / "system" / "apps" / package / "uv.lock").write_text(self.relocked_lock_text)
        return None

    def verify_app(self, worktree: Path, package: str) -> str | None:
        self.calls.append(f"verify_app {package}")
        return self.verify_failure

    def free_disk_bytes(self, directory: Path) -> int:
        return self.free_bytes

    def has_recent_backup(self) -> bool:
        self.calls.append("has_recent_backup")
        return self.is_backup_recent

    def remove_registry_row(self, name: str) -> str | None:
        self.calls.append(f"remove_registry_row {name}")
        return self.registry_failure

    def commit_paths(self, relative_paths: Sequence[str], message: str) -> str | None:
        self.calls.append(f"commit_paths {sorted(relative_paths)} {message!r}")
        return self.commit_failure

    def delete_ref(self, ref: str) -> None:
        self.calls.append(f"delete_ref {ref}")

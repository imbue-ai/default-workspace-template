from collections.abc import Sequence
from pathlib import Path

from pydantic import Field

from app_install.errors import DevRepositoryError
from app_install.errors import PublishedNameTakenError
from app_install.interfaces import CreatedRepository
from app_install.interfaces import SecretScan
from app_install.interfaces import WorkspaceRuntimeInterface
from app_install.runtime import WorkspaceRuntime
from app_manifest.manifest import EnvironmentNote


class RecordingRuntime(WorkspaceRuntimeInterface):
    """A workspace runtime that does nothing outside the tree and records what it was asked to do."""

    calls: list[str] = Field(default_factory=list, description="Every call, in order, as one line each")
    reload_failure: str | None = Field(default=None, description="What reloading programs fails with, if it does")
    registry_failure: str | None = Field(default=None, description="What removing a row fails with, if it does")
    ref_failure: str | None = Field(default=None, description="What deleting a ref fails with, if it does")
    commit_failure: str | None = Field(default=None, description="What a commit fails with, if it does")
    is_committing: bool = Field(default=False, description="Whether a commit is really made in the repository")
    is_port_answering: bool = Field(default=True, description="Whether a started app answers on its port")
    relocked_lock_text: str = Field(default="relocked\n", description="What a relock writes as the app's lock")
    verify_failure: str | None = Field(default=None, description="Why the app's checks fail, if they do")
    is_backup_recent: bool = Field(default=False, description="Whether the workspace has a recent backup")
    free_bytes: int = Field(default=10**12, description="How much room the disk has")
    unresolved_environment: tuple[str, ...] = Field(default=(), description="What the environment install fails on")
    github_account: str = Field(default="octo", description="The account a created repository lands under")
    repository_failure: str | None = Field(default=None, description="What creating a repository fails with")
    lockdown_problem: str = Field(default="", description="Why closing a created repository did not take")
    taken_repository_names: tuple[str, ...] = Field(default=(), description="The names the account already uses")
    secret_marker: str = Field(default="", description="Text the secret scan reports every file holding as a find")
    scan_failure: str = Field(default="", description="Why the secret scan cannot run, if it cannot")
    lock_failure: str | None = Field(default=None, description="Why a lock check fails, if it does")
    lock_checked_directories: list[Path] = Field(default_factory=list, description="Where a lock was checked")
    scanned_file_names: list[tuple[str, ...]] = Field(
        default_factory=list, description="The files each secret scan found in its directory, relative to it"
    )

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
        if self.is_committing:
            return WorkspaceRuntime(repo_root=self.repo_root).commit_paths(relative_paths, message)
        return self.commit_failure

    def delete_ref(self, ref: str) -> str | None:
        self.calls.append(f"delete_ref {ref}")
        return self.ref_failure

    def create_private_repository(self, name: str, description: str) -> CreatedRepository:
        self.calls.append(f"create_private_repository {name} {description!r}")
        if self.repository_failure is not None:
            raise DevRepositoryError(self.repository_failure)
        return CreatedRepository(
            url=f"https://github.com/{self.github_account}/{name}", lockdown_problem=self.lockdown_problem
        )

    def create_published_repository(self, name: str, description: str) -> CreatedRepository:
        self.calls.append(f"create_published_repository {name} {description!r}")
        if name in self.taken_repository_names:
            raise PublishedNameTakenError(f"the repo was not created: name already exists on this account ({name})")
        return CreatedRepository(
            url=f"https://github.com/{self.github_account}/{name}", lockdown_problem=self.lockdown_problem
        )

    def scan_secrets(self, directory: Path) -> SecretScan:
        self.calls.append("scan_secrets")
        files = sorted(path for path in directory.rglob("*") if path.is_file())
        self.scanned_file_names.append(tuple(path.relative_to(directory).as_posix() for path in files))
        if self.scan_failure:
            return SecretScan(failure=self.scan_failure)
        if not self.secret_marker:
            return SecretScan()
        return SecretScan(
            findings=tuple(
                f"[stand-in] planted-secret: {path.relative_to(directory).as_posix()} (value redacted)"
                for path in files
                if self.secret_marker in path.read_text(errors="replace")
            )
        )

    def check_lock(self, directory: Path) -> str | None:
        self.calls.append("check_lock")
        self.lock_checked_directories.append(directory)
        return self.lock_failure

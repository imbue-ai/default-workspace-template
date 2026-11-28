"""The installer's hands on the running workspace: supervisorctl, the registration script, git, and latchkey."""

import fnmatch
import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from app_install.derived import is_python_app
from app_install.errors import DevRepositoryError
from app_install.errors import PublishedNameTakenError
from app_install.errors import PublishError
from app_install.git import installer_identity_environment
from app_install.interfaces import CreatedRepository
from app_install.interfaces import SecretScan
from app_install.interfaces import WorkspaceRuntimeInterface
from app_install.paths import APPS_DIRECTORY
from app_manifest.manifest import EnvironmentNote
from app_registration import forward_port
from github_sync.config import GITHUB_URL_PREFIX
from github_sync.repositories import GithubRequestError
from github_sync.repositories import RepositoryNameTakenError
from github_sync.repositories import create_repository
from github_sync.repositories import lock_down_repository
from github_sync.repositories import make_repository_public
from github_sync.repositories import set_repository_topics
from host_backup.cli import has_recent_backup

# Long enough for a system package or a tool to download and build.
_COMMAND_TIMEOUT_SECONDS: Final[float] = 900.0
# How long a newly started app is given to answer on its port: it links and syncs its own environment first.
_PORT_WAIT_SECONDS: Final[float] = 120.0
_PORT_POLL_SECONDS: Final[float] = 0.5
_NOTHING_TO_COMMIT_MARKERS: Final[tuple[str, ...]] = ("nothing to commit", "no changes added to commit")
# The file names pytest collects by default, which every app in the template keeps to.
_TEST_FILE_PATTERNS: Final[tuple[str, ...]] = ("test_*.py", "*_test.py")
_NODE_MODULES_DIRNAME: Final[str] = "node_modules"
# The scan a template publish runs: two scanners, both of which must pass, and neither prints a value.
SECRET_SCAN_SCRIPT: Final[Path] = Path(".agents/skills/publish-template/scripts/scan_secrets.sh")
_SECRET_FINDING_MARKER: Final[str] = "SECRET SCAN FINDING "
# What published apps are found by as a group.
PUBLISHED_APP_TOPIC: Final[str] = "imbue-studio-app"


def has_test_files(directory: Path) -> bool:
    """Whether the app at ``directory`` carries a test file of its own. Hidden directories (the app's virtual
    environment) and ``node_modules`` hold other packages' tests and are not looked in."""
    for _current, directory_names, file_names in os.walk(directory):
        directory_names[:] = [
            name for name in directory_names if not name.startswith(".") and name != _NODE_MODULES_DIRNAME
        ]
        if any(fnmatch.fnmatch(name, pattern) for name in file_names for pattern in _TEST_FILE_PATTERNS):
            return True
    return False


class WorkspaceRuntime(WorkspaceRuntimeInterface):
    """The real workspace, reached through the commands an agent would run."""

    def _run(self, argv: Sequence[str]) -> str | None:
        """The failure's text, or None when the command succeeded."""
        return self._run_with_environment(argv, dict(os.environ))

    def _run_with_environment(self, argv: Sequence[str], environment: Mapping[str, str]) -> str | None:
        try:
            completed = subprocess.run(
                list(argv),
                cwd=self.repo_root,
                env=dict(environment),
                capture_output=True,
                text=True,
                check=False,
                timeout=_COMMAND_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"could not run `{' '.join(argv)}`: {e}"
        if completed.returncode != 0:
            return (
                completed.stderr or completed.stdout
            ).strip() or f"`{' '.join(argv)}` exited {completed.returncode}"
        return None

    def reload_programs(self) -> str | None:
        return self._run(["supervisorctl", "reread"]) or self._run(["supervisorctl", "update"])

    def wait_for_port(self, port: int) -> bool:
        deadline = time.monotonic() + _PORT_WAIT_SECONDS
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=_PORT_POLL_SECONDS):
                    return True
            except OSError:
                threading.Event().wait(_PORT_POLL_SECONDS)
        return False

    def install_environment(self, environment: EnvironmentNote) -> tuple[str, ...]:
        commands: list[tuple[str, list[str]]] = []
        if environment.apt:
            names = [str(name) for name in environment.apt]
            commands.append(
                (f"apt: {' '.join(names)}", ["apt-get", "install", "-y", "--no-install-recommends", *names])
            )
        if environment.cargo_default_toolchain is not None:
            toolchain = str(environment.cargo_default_toolchain)
            commands.append((f"rustup default {toolchain}", ["rustup", "default", toolchain]))
        commands.extend(
            (f"npm: {name}@{version}", ["npm", "install", "-g", f"{name}@{version}"])
            for name, version in sorted(environment.npm_global.items())
        )
        commands.extend(
            (f"uv tool: {name}=={version}", ["uv", "tool", "install", f"{name}=={version}"])
            for name, version in sorted(environment.uv_tools.items())
        )
        commands.extend(
            (f"cargo: {name}@{version}", ["cargo", "install", "--locked", f"{name}@{version}"])
            for name, version in sorted(environment.cargo_crates.items())
        )
        unresolved: list[str] = []
        for label, argv in commands:
            failure = self._run(argv)
            if failure is not None:
                unresolved.append(f"{label} ({failure.splitlines()[-1] if failure else 'failed'})")
        return tuple(unresolved)

    def restart_programs(self, programs: Sequence[str]) -> str | None:
        return self._run(["supervisorctl", "restart", *programs])

    def _run_in(self, directory: Path, argv: Sequence[str]) -> str | None:
        return WorkspaceRuntime(repo_root=directory)._run(argv)

    def relock_app(self, worktree: Path, package: str) -> str | None:
        if not is_python_app(worktree, package):
            return None
        return self._run_in(worktree, ["uv", "lock", "--project", (APPS_DIRECTORY / package).as_posix()])

    def verify_app(self, worktree: Path, package: str) -> str | None:
        if not is_python_app(worktree, package):
            return None
        relative = (APPS_DIRECTORY / package).as_posix()
        sync_failure = self._run_in(worktree, ["uv", "sync", "--locked", "--project", relative])
        if sync_failure is not None or not has_test_files(worktree / relative):
            return sync_failure
        return self._run_in(worktree / relative, ["uv", "run", "--no-sync", "pytest", "-q", "-p", "no:cacheprovider"])

    def free_disk_bytes(self, directory: Path) -> int:
        return shutil.disk_usage(directory).free

    def has_recent_backup(self) -> bool:
        return has_recent_backup()

    def remove_registry_row(self, name: str) -> str | None:
        try:
            forward_port.remove(forward_port.apps_file(self.repo_root), name)
        except (forward_port.RegistrationError, OSError) as e:
            return str(e)
        return None

    def _is_known_to_git(self, relative_path: str) -> bool:
        """Whether git can stage the path: it is on disk, or the index still holds it."""
        path = self.repo_root / relative_path
        if path.exists() or path.is_symlink():
            return True
        return self._run(["git", "ls-files", "--error-unmatch", "--", relative_path]) is None

    def commit_paths(self, relative_paths: Sequence[str], message: str) -> str | None:
        # git refuses a whole `add` over one path it has never seen, and a removed file that no commit held is
        # exactly that.
        relative_paths = [path for path in relative_paths if self._is_known_to_git(path)]
        if not relative_paths:
            return None
        staged = self._run(["git", "add", "--all", "--", *relative_paths])
        if staged is not None:
            return staged
        # With hooks on, unlike the installer's other git commands: this commit is on the primary checkout, where
        # GitHub sync's post-commit hook is what pushes it.
        committed = self._run_with_environment(
            ["git", "commit", "--quiet", "--only", "-m", message, "--", *relative_paths],
            installer_identity_environment(),
        )
        if committed is not None and any(marker in committed for marker in _NOTHING_TO_COMMIT_MARKERS):
            return None
        return committed

    def delete_ref(self, ref: str) -> str | None:
        return self._run(["git", "update-ref", "-d", ref])

    def create_private_repository(self, name: str, description: str) -> CreatedRepository:
        try:
            repository = create_repository(name, description, is_private=True)
        except GithubRequestError as e:
            raise DevRepositoryError(str(e)) from e
        return CreatedRepository(
            url=f"{GITHUB_URL_PREFIX}{repository}", lockdown_problem=lock_down_repository(repository) or ""
        )

    def create_published_repository(self, name: str, description: str, is_private: bool) -> CreatedRepository:
        try:
            repository = create_repository(name, description, is_private=is_private)
        except RepositoryNameTakenError as e:
            raise PublishedNameTakenError(str(e)) from e
        except GithubRequestError as e:
            raise PublishError(str(e)) from e
        topic_problem = ""
        try:
            set_repository_topics(repository, [PUBLISHED_APP_TOPIC])
        except GithubRequestError as e:
            topic_problem = str(e)
        return CreatedRepository(
            url=f"{GITHUB_URL_PREFIX}{repository}",
            lockdown_problem=lock_down_repository(repository) or "",
            topic_problem=topic_problem,
        )

    def make_published_repository_public(self, repo_url: str) -> None:
        try:
            make_repository_public(repo_url.removeprefix(GITHUB_URL_PREFIX))
        except GithubRequestError as e:
            raise PublishError(str(e)) from e

    def scan_secrets(self, directory: Path) -> SecretScan:
        script = self.repo_root / SECRET_SCAN_SCRIPT
        try:
            completed = subprocess.run(
                ["bash", str(script), str(directory)],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=_COMMAND_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return SecretScan(failure=f"could not run {SECRET_SCAN_SCRIPT.as_posix()}: {e}")
        if completed.returncode == 0:
            return SecretScan()
        findings = tuple(
            line.partition(_SECRET_FINDING_MARKER)[2].strip()
            for line in completed.stderr.splitlines()
            if _SECRET_FINDING_MARKER in line
        )
        if findings:
            return SecretScan(findings=findings)
        # A scan that fails with nothing found did not run to the end: a scanner is missing or broke.
        return SecretScan(
            failure=completed.stderr.strip() or f"{SECRET_SCAN_SCRIPT.as_posix()} exited {completed.returncode}"
        )

    def check_lock(self, directory: Path) -> str | None:
        return self._run_in(directory, ["uv", "lock", "--check"])

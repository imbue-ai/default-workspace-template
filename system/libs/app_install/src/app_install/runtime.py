"""The installer's hands on the running workspace: supervisorctl, the registration script, and git."""

import socket
import subprocess
import threading
import time
from collections.abc import Sequence
from typing import Final

from app_install.git import COMMITTER_EMAIL
from app_install.git import COMMITTER_NAME
from app_install.interfaces import WorkspaceRuntimeInterface
from app_manifest.manifest import EnvironmentNote

_FORWARD_PORT: Final[str] = "system/scripts/forward_port.py"
# Long enough for a system package or a tool to download and build.
_COMMAND_TIMEOUT_SECONDS: Final[float] = 900.0
# How long a newly started app is given to answer on its port: it links and syncs its own environment first.
_PORT_WAIT_SECONDS: Final[float] = 120.0
_PORT_POLL_SECONDS: Final[float] = 0.5
_NOTHING_TO_COMMIT_MARKERS: Final[tuple[str, ...]] = ("nothing to commit", "no changes added to commit")


class WorkspaceRuntime(WorkspaceRuntimeInterface):
    """The real workspace, reached through the commands an agent would run."""

    def _run(self, argv: Sequence[str]) -> str | None:
        """The failure's text, or None when the command succeeded."""
        try:
            completed = subprocess.run(
                list(argv),
                cwd=self.repo_root,
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

    def remove_registry_row(self, name: str) -> str | None:
        return self._run(["python3", _FORWARD_PORT, "--remove", "--name", name])

    def commit_paths(self, relative_paths: Sequence[str], message: str) -> str | None:
        staged = self._run(["git", "add", "--all", "--", *relative_paths])
        if staged is not None:
            return staged
        committed = self._run(
            [
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
                *relative_paths,
            ]
        )
        if committed is not None and any(marker in committed for marker in _NOTHING_TO_COMMIT_MARKERS):
            return None
        return committed

    def delete_ref(self, ref: str) -> None:
        # A ref that does not exist is the state asked for.
        self._run(["git", "update-ref", "-d", ref])

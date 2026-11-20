"""The installer's hands on the running workspace: supervisorctl, the registration script, and git."""

import subprocess
from collections.abc import Sequence
from typing import Final

from app_install.interfaces import WorkspaceRuntimeInterface

# The installer's own committer: its commits come from a command, not from any one chat's agent, and the
# analytics feed tells them from the user's own work by this name.
COMMITTER_NAME: Final[str] = "app-install"
COMMITTER_EMAIL: Final[str] = "app-install@workspace.invalid"
_FORWARD_PORT: Final[str] = "system/scripts/forward_port.py"
_COMMAND_TIMEOUT_SECONDS: Final[float] = 120.0
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

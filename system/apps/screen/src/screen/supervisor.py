"""The service's view of supervisord, through ``supervisorctl`` (the socket is root's, and so is the service)."""

import subprocess
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.pure import pure
from screen.errors import SupervisorError
from screen.interfaces import ProgramState
from screen.interfaces import SupervisorInterface
from screen.primitives import STACK_GROUP

# A start or stop of one program is a local socket round trip plus the program's startsecs; a status read is
# instant. Hard timeouts well past both, warning thresholds where they start to look wrong.
_COMMAND_TIMEOUT_SECONDS: Final[float] = 60.0
_STATUS_TIMEOUT_SECONDS: Final[float] = 15.0

# What ``supervisorctl start`` and ``stop`` print for the no-op cases, which are not errors here.
_ALREADY_STARTED_MARKER: Final[str] = "ERROR (already started)"
_NOT_RUNNING_MARKER: Final[str] = "ERROR (not running)"


@pure
def group_member(program: str) -> str:
    """How supervisorctl names a program of the stack's group."""
    return f"{STACK_GROUP}:{program}"


@pure
def parse_status_line(line: str) -> ProgramState:
    """The state in one ``supervisorctl status`` line (``screen-stack:screen-xorg   RUNNING   pid 12, uptime 0:00:03``)."""
    fields = line.split()
    if len(fields) < 2:
        return ProgramState.UNKNOWN
    try:
        return ProgramState(fields[1])
    except ValueError:
        return ProgramState.UNKNOWN


class SupervisorctlClient(SupervisorInterface):
    """Runs ``supervisorctl`` for the group's programs."""

    supervisorctl: str = Field(default="supervisorctl", frozen=True, description="The supervisorctl executable")

    def _run(self, arguments: list[str], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.supervisorctl, *arguments],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise SupervisorError(f"supervisorctl {' '.join(arguments)} failed: {e}") from e

    def start_program(self, program: str) -> None:
        completed = self._run(["start", group_member(program)], _COMMAND_TIMEOUT_SECONDS)
        output = completed.stdout + completed.stderr
        if completed.returncode != 0 and _ALREADY_STARTED_MARKER not in output:
            raise SupervisorError(f"could not start {program}: {output.strip()}")
        logger.debug("Asked supervisord to start {}: {}", program, output.strip())

    def stop_program(self, program: str) -> None:
        completed = self._run(["stop", group_member(program)], _COMMAND_TIMEOUT_SECONDS)
        output = completed.stdout + completed.stderr
        if completed.returncode != 0 and _NOT_RUNNING_MARKER not in output:
            raise SupervisorError(f"could not stop {program}: {output.strip()}")
        logger.debug("Asked supervisord to stop {}: {}", program, output.strip())

    def program_state(self, program: str) -> ProgramState:
        # ``status`` exits non-zero whenever a listed program is not RUNNING, so the exit code says nothing.
        completed = self._run(["status", group_member(program)], _STATUS_TIMEOUT_SECONDS)
        return parse_status_line(completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else "")

    def program_pid(self, program: str) -> int | None:
        # ``pid`` prints the pid, or 0 for a program that is not running.
        completed = self._run(["pid", group_member(program)], _STATUS_TIMEOUT_SECONDS)
        text = completed.stdout.strip()
        if completed.returncode != 0 or not text.isdigit() or int(text) == 0:
            return None
        return int(text)

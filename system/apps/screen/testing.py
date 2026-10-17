"""Shared helpers for the Screen's tests."""

import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from pydantic import Field

from screen.data_types import Lifecycle
from screen.errors import SupervisorError
from screen.interfaces import ProgramState
from screen.interfaces import SupervisorInterface
from screen.neko_config import PULSE_SOCKET_PATH
from screen.neko_config import RUNTIME_DIR
from screen.primitives import DISPLAY
from screen.primitives import NEKO_PROGRAM
from screen.primitives import PULSE_PROGRAM
from screen.primitives import SESSION_PROGRAM
from screen.primitives import STACK_PROGRAMS
from screen.primitives import XORG_PROGRAM
from screen.service import ScreenService
from screen.stack import NEKO_BINARY_PATH
from screen.stack import NEKO_YAML_FILENAME
from screen.stack import PULSE_SCRIPT_FILENAME
from screen.stack import XORG_CONF_FILENAME

_DEFAULT_WAIT_SECONDS: Final[float] = 10.0
_POLL_SECONDS: Final[float] = 0.02


class ManualClock:
    """A monotonic clock a test advances by hand, standing in for ``time.monotonic``."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def wait_until(predicate: Callable[[], bool], timeout_seconds: float = _DEFAULT_WAIT_SECONDS) -> bool:
    """Poll ``predicate`` until it holds or the deadline passes."""
    deadline = time.monotonic() + timeout_seconds
    pause = threading.Event()
    while time.monotonic() < deadline:
        if predicate():
            return True
        pause.wait(_POLL_SECONDS)
    return predicate()


def start_service_stack(service: ScreenService) -> None:
    """Start the stack through its on-demand path and wait until it runs."""
    service.ensure_started()
    assert wait_until(lambda: service.stack.lifecycle == Lifecycle.RUNNING), service.stack.stop_reason


class SubprocessSupervisor(SupervisorInterface):
    """The group's programs as plain subprocesses with the drop-ins' commands, for a test that runs the real stack
    where no supervisord does (the workspace image's CI run)."""

    model_config = {"arbitrary_types_allowed": True}

    state_dir: Path = Field(frozen=True, description="Where the rendered configuration is")
    log_dir: Path = Field(frozen=True, description="Where each program's output goes")
    processes: dict[str, subprocess.Popen[bytes]] = Field(default_factory=dict, description="Live processes")

    def _command(self, program: str) -> list[str]:
        if program == XORG_PROGRAM:
            return [
                "Xorg",
                DISPLAY,
                "-config",
                str(self.state_dir / XORG_CONF_FILENAME),
                "-noreset",
                "-nolisten",
                "tcp",
                "-novtswitch",
                "-logfile",
                str(self.log_dir / "Xorg.log"),
            ]
        if program == PULSE_PROGRAM:
            return [
                "pulseaudio",
                "--daemonize=no",
                "--exit-idle-time=-1",
                "--disallow-exit",
                "--disallow-module-loading",
                "-nF",
                str(self.state_dir / PULSE_SCRIPT_FILENAME),
            ]
        if program == SESSION_PROGRAM:
            return ["startxfce4"]
        if program == NEKO_PROGRAM:
            return [str(NEKO_BINARY_PATH), "serve", "--config", str(self.state_dir / NEKO_YAML_FILENAME)]
        raise SupervisorError(f"no command for {program}")

    def start_program(self, program: str) -> None:
        if program in self.processes and self.processes[program].poll() is None:
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        environment = {
            **os.environ,
            "DISPLAY": DISPLAY,
            "XDG_RUNTIME_DIR": RUNTIME_DIR,
            "PULSE_SERVER": f"unix:{PULSE_SOCKET_PATH}",
        }
        with (self.log_dir / f"{program}.log").open("ab") as log:
            self.processes[program] = subprocess.Popen(
                self._command(program), env=environment, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )

    def stop_program(self, program: str) -> None:
        process = self.processes.get(program)
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    def program_state(self, program: str) -> ProgramState:
        process = self.processes.get(program)
        if process is None:
            return ProgramState.STOPPED
        if process.poll() is None:
            return ProgramState.RUNNING
        return ProgramState.EXITED if process.returncode != 0 or program in self.processes else ProgramState.STOPPED

    def kill_program(self, program: str) -> None:
        """End a program the way memory pressure would."""
        process = self.processes[program]
        process.kill()
        process.wait(timeout=10)

    def stop_everything(self) -> None:
        for program in reversed(STACK_PROGRAMS):
            self.stop_program(program)

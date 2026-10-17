import os
import threading
from pathlib import Path

from pydantic import Field
from pydantic import PrivateAttr

from screen.errors import SupervisorError
from screen.interfaces import ProgramState
from screen.interfaces import SupervisorInterface
from screen.primitives import XORG_PROGRAM


class FakeSupervisor(SupervisorInterface):
    """Programs that start and stop at once; starting Xorg creates the display socket the stack waits for."""

    display_socket_path: Path | None = Field(default=None, description="Created when screen-xorg starts")
    failing_programs: set[str] = Field(default_factory=set, description="Programs whose start raises")
    states: dict[str, ProgramState] = Field(default_factory=dict, description="Each program's state")
    calls: list[tuple[str, str]] = Field(default_factory=list, description="Every (verb, program) in order")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def start_program(self, program: str) -> None:
        with self._lock:
            self.calls.append(("start", program))
            if program in self.failing_programs:
                self.states[program] = ProgramState.FATAL
                raise SupervisorError(f"{program} refused to start")
            self.states[program] = ProgramState.RUNNING
        if program == XORG_PROGRAM and self.display_socket_path is not None:
            self.display_socket_path.parent.mkdir(parents=True, exist_ok=True)
            self.display_socket_path.touch()

    def stop_program(self, program: str) -> None:
        with self._lock:
            self.calls.append(("stop", program))
            self.states[program] = ProgramState.STOPPED
        if program == XORG_PROGRAM and self.display_socket_path is not None and self.display_socket_path.exists():
            os.unlink(self.display_socket_path)

    def program_state(self, program: str) -> ProgramState:
        with self._lock:
            return self.states.get(program, ProgramState.STOPPED)

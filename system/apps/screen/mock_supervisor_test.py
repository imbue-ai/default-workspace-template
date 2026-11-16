import threading
from pathlib import Path

from pydantic import Field
from pydantic import PrivateAttr

from screen.errors import SupervisorError
from screen.interfaces import ProgramState
from screen.interfaces import SupervisorInterface
from screen.primitives import XORG_PROGRAM


class FakeSupervisor(SupervisorInterface):
    """Programs that start and stop at once; starting Xorg creates the display socket and the input driver's socket
    the stack waits for."""

    display_socket_path: Path | None = Field(default=None, description="Created when screen-xorg starts")
    input_socket_path: Path | None = Field(default=None, description="Created when screen-xorg starts")
    failing_programs: set[str] = Field(default_factory=set, description="Programs whose start raises")
    states: dict[str, ProgramState] = Field(default_factory=dict, description="Each program's state")
    pids: dict[str, int] = Field(default_factory=dict, description="The pid a running program answers")
    calls: list[tuple[str, str]] = Field(default_factory=list, description="Every (verb, program) in order")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def start_program(self, program: str) -> None:
        with self._lock:
            self.calls.append(("start", program))
            if program in self.failing_programs:
                self.states[program] = ProgramState.FATAL
                raise SupervisorError(f"{program} refused to start")
            self.states[program] = ProgramState.RUNNING
        if program == XORG_PROGRAM:
            for socket_path in (self.display_socket_path, self.input_socket_path):
                if socket_path is not None:
                    socket_path.parent.mkdir(parents=True, exist_ok=True)
                    socket_path.touch()

    def stop_program(self, program: str) -> None:
        with self._lock:
            self.calls.append(("stop", program))
            self.states[program] = ProgramState.STOPPED
        # An abandoned start and the stop that abandoned it may both stop Xorg, so the unlink races itself.
        if program == XORG_PROGRAM:
            for socket_path in (self.display_socket_path, self.input_socket_path):
                if socket_path is not None:
                    socket_path.unlink(missing_ok=True)

    def program_state(self, program: str) -> ProgramState:
        with self._lock:
            return self.states.get(program, ProgramState.STOPPED)

    def program_pid(self, program: str) -> int | None:
        with self._lock:
            if self.states.get(program, ProgramState.STOPPED) != ProgramState.RUNNING:
                return None
            return self.pids.get(program)

from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from enum import auto

from pydantic import SecretStr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.mutable_model import MutableModel
from screen.data_types import NekoSession
from screen.data_types import NekoSessionRecord
from screen.data_types import ScreenSize
from screen.data_types import XWindow
from screen.primitives import NekoSessionId
from screen.primitives import NekoToken
from screen.primitives import XWindowId


class ProgramState(UpperCaseStrEnum):
    """A supervisord program's state, as ``supervisorctl status`` names it."""

    STOPPED = auto()
    STARTING = auto()
    RUNNING = auto()
    BACKOFF = auto()
    STOPPING = auto()
    EXITED = auto()
    FATAL = auto()
    UNKNOWN = auto()


class SupervisorInterface(MutableModel, ABC):
    """What the service asks supervisord: start and stop the stack's programs and read their state."""

    @abstractmethod
    def start_program(self, program: str) -> None:
        """Start a program of the stack's group; a program already running is left running."""

    @abstractmethod
    def stop_program(self, program: str) -> None:
        """Stop a program of the stack's group; a program already stopped is left stopped."""

    @abstractmethod
    def program_state(self, program: str) -> ProgramState:
        """The program's current state."""


class WindowToolsInterface(MutableModel, ABC):
    """What the service asks the display: the window list, the active window's title, a raise."""

    @abstractmethod
    def list_windows(self) -> list[XWindow]:
        """Every managed X window on the display."""

    @abstractmethod
    def active_window_title(self) -> str | None:
        """The frontmost window's title, or None when no window is active."""

    @abstractmethod
    def activate_window(self, window_id: XWindowId) -> None:
        """Raise and focus a window."""


class AgentMessengerInterface(MutableModel, ABC):
    """How the lease wakes a queued agent: a message to its chat."""

    @abstractmethod
    def message_agent(self, agent_id: str, agent_name: str | None, text: str) -> None:
        """Deliver ``text`` to the agent's chat; failures are logged, never raised."""


class ProcessLauncherInterface(MutableModel, ABC):
    """How ``screen run`` starts a command on the display."""

    @abstractmethod
    def launch(self, command: Sequence[str]) -> int:
        """Start the command detached on the Screen and return its pid."""


class NekoInterface(MutableModel, ABC):
    """What the service asks neko over its REST API, with the API token or a session's token."""

    @abstractmethod
    def set_api_token(self, token: SecretStr | None) -> None:
        """Use this stack start's API token for the admin routes; None between starts."""

    @abstractmethod
    def is_healthy(self) -> bool:
        """Whether neko's health route answers."""

    @abstractmethod
    def login(self, username: str, password: str, is_owner: bool) -> NekoSession:
        """Log a member in and return its session (the multiuser provider admits any name with a known password)."""

    @abstractmethod
    def logout(self, token: NekoToken) -> None:
        """Log a session out; neko drops a multiuser session on logout."""

    @abstractmethod
    def set_locked_controls(self, is_locked: bool) -> None:
        """Flip neko's ``locked_controls`` setting (the agent lease's projection)."""

    @abstractmethod
    def give_control(self, session_id: NekoSessionId) -> None:
        """Make a session the host."""

    @abstractmethod
    def reset_control(self) -> None:
        """Clear the host."""

    @abstractmethod
    def screenshot_jpeg(self, quality: int) -> bytes:
        """The display as a JPEG at the given quality."""

    @abstractmethod
    def screen_size(self) -> ScreenSize:
        """The display's current mode."""

    @abstractmethod
    def list_sessions(self) -> list[NekoSessionRecord]:
        """Every session neko holds, with its connection state."""

    @abstractmethod
    def delete_session(self, session_id: NekoSessionId) -> None:
        """Delete a session outright."""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from imbue.imbue_common.mutable_model import MutableModel

from terminal_app.data_types import TerminalSessionRecord, TmuxSession
from terminal_app.primitives import TmuxSessionId, TmuxSessionName, Workdir


class TmuxInterface(MutableModel, ABC):
    """The default tmux server, as the terminal app drives it."""

    @abstractmethod
    def list_sessions(self) -> list[TmuxSession]:
        """Every session on the server; none when no server is running."""

    @abstractmethod
    def create_session(
        self, name: TmuxSessionName, workdir: Workdir, command: Sequence[str]
    ) -> TmuxSession:
        """Create a detached session running ``command`` in ``workdir`` and return it (its id and creation time); raises TmuxCommandError when tmux refuses (a name already taken included)."""

    @abstractmethod
    def kill_session(self, target: TmuxSessionName | TmuxSessionId) -> None:
        """Kill the session; an absent session is not an error, one that survives raises TmuxCommandError."""


class TerminalSessionStoreInterface(MutableModel, ABC):
    """The app's own record of its terminals, which outlives the tmux server."""

    @abstractmethod
    def list_records(self) -> list[TerminalSessionRecord]:
        """Every remembered terminal, in creation order."""

    @abstractmethod
    def save_record(self, record: TerminalSessionRecord) -> None:
        """Remember a terminal, replacing any record with the same name in its place."""

    @abstractmethod
    def remove_record(self, name: TmuxSessionName) -> None:
        """Forget a terminal; an absent name is not an error."""

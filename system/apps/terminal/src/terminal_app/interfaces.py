from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any

from imbue.imbue_common.mutable_model import MutableModel

from terminal_app.data_types import TerminalSessionRecord, TmuxClient, TmuxSession
from terminal_app.primitives import TmuxSessionId, TmuxSessionName, Workdir


class TmuxInterface(MutableModel, ABC):
    """The default tmux server, as the terminal app drives it."""

    @abstractmethod
    def list_sessions(self) -> list[TmuxSession]:
        """Every session on the server; none when no server is running."""

    @abstractmethod
    def list_clients(self) -> list[TmuxClient]:
        """Every attached client; none when no server is running."""

    @abstractmethod
    def create_session(
        self, name: TmuxSessionName, workdir: Workdir, command: Sequence[str]
    ) -> TmuxSessionId:
        """Create a detached session running ``command`` in ``workdir`` and return its id; raises TmuxCommandError when tmux refuses (a name already taken included)."""

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


class ShellPosterInterface(MutableModel, ABC):
    """Posts to the shell's loopback routes on the terminal app's behalf."""

    @abstractmethod
    def post_json(self, path: str, body: Mapping[str, Any]) -> None:
        """POST ``body`` to the shell route at ``path``; must never raise for an unreachable or refusing shell."""

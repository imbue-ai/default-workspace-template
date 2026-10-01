from abc import ABC
from abc import abstractmethod

from imbue.imbue_common.mutable_model import MutableModel

from workspace_layout.answers import ClientView
from workspace_layout.answers import DesktopOpAnswer
from workspace_layout.answers import OpenAnswer
from workspace_layout.answers import ShowAnswer
from workspace_layout.answers import TransientOpAnswer
from workspace_layout.ops import ClientActivityReport
from workspace_layout.ops import NavigateRequest
from workspace_layout.ops import OpenRequest
from workspace_layout.ops import PlaceRequest
from workspace_layout.ops import ShowRequest
from workspace_layout.ops import WindowRequest
from workspace_layout.records import DesktopView


class ShellLayoutInterface(MutableModel, ABC):
    """What an app or agent asks of the shell's layout (desktop contracts.md sections 5 and 8). Every op raises a
    ShellOpError (unreachable, refused, or answered malformed) when the shell did not do what it was asked."""

    @abstractmethod
    def show(self, request: ShowRequest) -> ShowAnswer:
        """Put an app's path on a client's screen, the shell choosing the window, and answer how it did."""

    @abstractmethod
    def open(self, request: OpenRequest) -> OpenAnswer:
        """Open (or focus) an app's window at a path and answer the window."""

    @abstractmethod
    def focus(self, request: WindowRequest) -> DesktopOpAnswer:
        """Restore and raise a window for a client."""

    @abstractmethod
    def navigate(self, request: NavigateRequest) -> DesktopOpAnswer:
        """Point a window at another path under its app."""

    @abstractmethod
    def place(self, request: PlaceRequest) -> DesktopOpAnswer:
        """Set a window's frame for a client."""

    @abstractmethod
    def close(self, request: WindowRequest) -> DesktopOpAnswer:
        """Close a window for everyone."""

    @abstractmethod
    def refresh(self, request: WindowRequest) -> TransientOpAnswer:
        """Reload one window's page on the client it targets, and answer the client it reached."""

    @abstractmethod
    def connected_clients(self) -> list[ClientView]:
        """The clients holding a socket right now, in the shell's order."""

    @abstractmethod
    def desktops(self) -> list[DesktopView]:
        """Every desktop, in the shell's order (the first is the fallback desktop)."""

    @abstractmethod
    def record_client_activity(self, report: ClientActivityReport) -> None:
        """Tell the shell a client sent a message to a page; best-effort, never raises."""

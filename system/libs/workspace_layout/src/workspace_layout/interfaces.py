from abc import ABC
from abc import abstractmethod

from imbue.imbue_common.mutable_model import MutableModel

from workspace_layout.answers import ClientView
from workspace_layout.answers import DesktopOpAnswer
from workspace_layout.answers import OpenAnswer
from workspace_layout.answers import ShowAnswer
from workspace_layout.answers import TransientOpAnswer
from workspace_layout.ops import ClientActivityReport
from workspace_layout.ops import NavigateArgs
from workspace_layout.ops import OpenArgs
from workspace_layout.ops import PlaceArgs
from workspace_layout.ops import RefreshArgs
from workspace_layout.ops import ShowArgs
from workspace_layout.ops import WindowArgs
from workspace_layout.records import DesktopView


class ShellLayoutInterface(MutableModel, ABC):
    """What an app or agent asks of the shell's layout (desktop contracts.md sections 5 and 8). Every op raises a
    ShellOpError (unreachable, refused, or answered malformed) when the shell did not do what it was asked."""

    @abstractmethod
    def show(self, args: ShowArgs) -> ShowAnswer:
        """Put an app's path on a client's screen, the shell choosing the window, and answer how it did."""

    @abstractmethod
    def open(self, args: OpenArgs) -> OpenAnswer:
        """Open (or focus) an app's window at a path and answer the window."""

    @abstractmethod
    def focus(self, args: WindowArgs) -> DesktopOpAnswer:
        """Restore and raise a window for a client."""

    @abstractmethod
    def navigate(self, args: NavigateArgs) -> DesktopOpAnswer:
        """Point a window at another path under its app."""

    @abstractmethod
    def place(self, args: PlaceArgs) -> DesktopOpAnswer:
        """Set a window's state (snapped to a half, or maximized) or its frame for a client."""

    @abstractmethod
    def close(self, args: WindowArgs) -> DesktopOpAnswer:
        """Close a window for everyone."""

    @abstractmethod
    def refresh(self, args: RefreshArgs) -> TransientOpAnswer:
        """Reload one window's page on the client it targets, or every page of an app; answer the client it reached."""

    @abstractmethod
    def connected_clients(self) -> list[ClientView]:
        """The clients holding a socket right now, in the shell's order."""

    @abstractmethod
    def desktops(self) -> list[DesktopView]:
        """Every desktop, in the shell's order (the first is the fallback desktop)."""

    @abstractmethod
    def record_client_activity(self, report: ClientActivityReport) -> None:
        """Tell the shell a client sent a message to a page; best-effort, never raises."""

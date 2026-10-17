from pydantic import Field

from screen.data_types import XWindow
from screen.interfaces import WindowToolsInterface
from screen.primitives import XWindowId


class FakeWindowTools(WindowToolsInterface):
    """A display whose windows and active title the test sets."""

    windows: list[XWindow] = Field(default_factory=list, description="What the list answers")
    active_window_id: str | None = Field(default=None, description="The id of the window the title poll finds active")
    activated: list[str] = Field(default_factory=list, description="Every window id raised, in order")

    def list_windows(self) -> list[XWindow]:
        return list(self.windows)

    def active_window(self) -> XWindow | None:
        return next((window for window in self.windows if window.id == self.active_window_id), None)

    def activate_window(self, window_id: XWindowId) -> None:
        self.activated.append(str(window_id))

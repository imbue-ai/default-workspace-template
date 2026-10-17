from pydantic import Field

from screen.data_types import XWindow
from screen.interfaces import WindowToolsInterface
from screen.primitives import XWindowId


class FakeWindowTools(WindowToolsInterface):
    """A display whose windows and active title the test sets."""

    windows: list[XWindow] = Field(default_factory=list, description="What the list answers")
    active_title: str | None = Field(default=None, description="What the title poll answers")
    activated: list[str] = Field(default_factory=list, description="Every window id raised, in order")

    def list_windows(self) -> list[XWindow]:
        return list(self.windows)

    def active_window_title(self) -> str | None:
        return self.active_title

    def activate_window(self, window_id: XWindowId) -> None:
        self.activated.append(str(window_id))

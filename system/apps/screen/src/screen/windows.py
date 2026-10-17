"""The X window list and the frontmost window, through ``wmctrl`` and ``xdotool`` on the Screen's display."""

import subprocess
from typing import Final

from pydantic import Field

from imbue.imbue_common.pure import pure
from screen.data_types import XWindow
from screen.errors import WindowToolError
from screen.interfaces import WindowToolsInterface
from screen.primitives import DISPLAY
from screen.primitives import XWindowId

_TOOL_TIMEOUT_SECONDS: Final[float] = 10.0

# ``wmctrl -lpxG`` prints: id, desktop, pid, x, y, width, height, class, host, then the title (which may hold
# spaces, so it is everything after the ninth field).
_WMCTRL_FIXED_FIELD_COUNT: Final[int] = 9


@pure
def parse_wmctrl_list(output: str) -> list[XWindow]:
    """The windows ``wmctrl -lpxG`` printed, one per line; a line that does not fit the shape is skipped."""
    windows: list[XWindow] = []
    for line in output.splitlines():
        fields = line.split(None, _WMCTRL_FIXED_FIELD_COUNT)
        if len(fields) < _WMCTRL_FIXED_FIELD_COUNT:
            continue
        title = fields[_WMCTRL_FIXED_FIELD_COUNT] if len(fields) > _WMCTRL_FIXED_FIELD_COUNT else ""
        try:
            windows.append(
                XWindow(
                    id=XWindowId(fields[0]),
                    desktop=int(fields[1]),
                    pid=int(fields[2]),
                    x=int(fields[3]),
                    y=int(fields[4]),
                    width=int(fields[5]),
                    height=int(fields[6]),
                    window_class=fields[7],
                    title=title.rstrip("\n"),
                )
            )
        except (ValueError, WindowToolError):
            continue
    return windows


@pure
def window_title_from_tool_output(output: str) -> str | None:
    """The title ``xdotool getactivewindow getwindowname`` printed, or None for an empty answer."""
    title = output.strip()
    return title if title else None


class DisplayWindowTools(WindowToolsInterface):
    """Runs wmctrl and xdotool against the Screen's display."""

    display: str = Field(default=DISPLAY, frozen=True, description="The X display the tools talk to")

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=_TOOL_TIMEOUT_SECONDS,
                check=False,
                env={"DISPLAY": self.display, "PATH": "/usr/local/bin:/usr/bin:/bin"},
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise WindowToolError(f"{command[0]} failed: {e}") from e

    def list_windows(self) -> list[XWindow]:
        completed = self._run(["wmctrl", "-lpxG"])
        if completed.returncode != 0:
            raise WindowToolError(f"wmctrl -l failed: {completed.stderr.strip()}")
        return parse_wmctrl_list(completed.stdout)

    def active_window_title(self) -> str | None:
        completed = self._run(["xdotool", "getactivewindow", "getwindowname"])
        # xdotool exits non-zero when no window is active (an empty desktop), which is an answer, not an error.
        if completed.returncode != 0:
            return None
        return window_title_from_tool_output(completed.stdout)

    def activate_window(self, window_id: XWindowId) -> None:
        completed = self._run(["wmctrl", "-i", "-a", str(window_id)])
        if completed.returncode != 0:
            raise WindowToolError(f"wmctrl could not activate {window_id}: {completed.stderr.strip()}")

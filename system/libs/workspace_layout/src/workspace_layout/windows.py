import urllib.parse
from typing import Any
from typing import Final

from app_manifest.manifest import describe_validation_error
from app_manifest.primitives import AppName
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from workspace_layout.answers import DesktopsListing
from workspace_layout.client import request_shell
from workspace_layout.errors import ShellUnreachableError
from workspace_layout.primitives import DesktopId
from workspace_layout.primitives import WindowId
from workspace_layout.primitives import WindowPath
from workspace_layout.shell_url import DESKTOPS_ROUTE
from workspace_layout.transport import quote_answer

# One loopback read of a small file the shell holds in memory; past this it is not answering.
WINDOW_READ_TIMEOUT_SECONDS: Final[float] = 2.0


def read_app_window_paths(shell_url: str, app: AppName) -> list[str] | None:
    """Every window path of ``app`` across every desktop (an independent window's home path and each client's own
    path of it), or None when the shell could not be read.

    None is never "no windows": an app that collects what no window shows must skip a sweep it
    cannot ground in the shell's own answer, so an unreachable shell, a non-JSON body, and a
    document of the wrong shape all read as unknown (docs/system/specs/window-bound-resources.md section 4.2).
    """
    url = f"{shell_url}{DESKTOPS_ROUTE}"
    try:
        response = request_shell("GET", url, None, WINDOW_READ_TIMEOUT_SECONDS)
    except ShellUnreachableError as e:
        logger.debug("Could not read the shell's desktops at {}: {}", url, e)
        return None
    if not response.is_success:
        logger.debug("The shell answered its desktops at {} with {}", url, response.status_code)
        return None
    if isinstance(response.body, str):
        logger.warning("The shell's desktops at {} are not a JSON object: {}", url, quote_answer(response.body))
        return None
    try:
        listing = DesktopsListing.model_validate(response.body, extra="ignore")
    except ValidationError as e:
        logger.warning("The shell's desktops at {} are not a desktops listing: {}", url, describe_validation_error(e))
        return None
    return _paths_of_app(listing, app)


@pure
def _paths_of_app(listing: DesktopsListing, app: AppName) -> list[str]:
    paths: list[str] = []
    for desktop in listing.desktops:
        for window in desktop.windows:
            if window.app == app:
                paths.append(str(window.path))
                # An independent window's shared path stays its home path; what each client's page shows rides
                # beside it, and any one of them keeps a resource alive.
                paths.extend(str(client_path) for client_path in window.client_paths.values())
    return paths


class WindowClosedHint(FrozenModel):
    """What the shell posts to an app's ``window_closed_path`` when a window of the app closes
    (docs/system/specs/window-bound-resources.md section 4.6)."""

    path: WindowPath = Field(description="The path the closed window's page was at")
    window_id: WindowId = Field(description="The window that closed")
    desktop_id: DesktopId = Field(description="The desktop it was on")


@pure
def parse_window_closed_hint(body: Any) -> WindowClosedHint | None:
    """The hint a closed-window post carries, or None for a body of another shape."""
    try:
        return WindowClosedHint.model_validate(body, extra="ignore")
    except ValidationError:
        return None


@pure
def window_query_value(path: str, name: str) -> str | None:
    """The first value of the query parameter ``name`` in a window path, or None when it carries none."""
    values = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get(name)
    return values[0] if values else None

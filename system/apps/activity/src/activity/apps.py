"""Stopping a desktop app, through the shell's own Quit (``POST /api/apps/<name>/quit``).

The shell owns an app's windows and its program: its Quit closes every window of the app on every desktop and stops
the program, and the shell starts it again on the next request, so a stopped app comes back when it is next opened.
This app asks the shell rather than calling ``supervisorctl`` itself, and offers Stop only for an app the shell would
quit, less the browser (agents drive it, so stopping it would pull it from under them) and this app itself (stopping
it would close the window asking).
"""

import urllib.parse
from collections.abc import Sequence
from typing import Final

import httpx

from activity.errors import ShellUnavailableError
from activity.replies import refusal_reason
from app_manifest.primitives import AppName
from app_manifest.registry import RegistryRow
from imbue.imbue_common.pure import pure

SELF_APP_NAME: Final[AppName] = AppName("activity")
BROWSER_APP_NAME: Final[AppName] = AppName("browser")
# A quit stops the program at once, but closing the app's windows on every desktop goes first.
APP_QUIT_TIMEOUT_SECONDS: Final[float] = 30.0
NEVER_STOPPED_APP_NAMES: Final[frozenset[AppName]] = frozenset({SELF_APP_NAME, BROWSER_APP_NAME})


@pure
def is_quittable_by_shell(row: RegistryRow, rows: Sequence[RegistryRow]) -> bool:
    """The shell's rule for what it may stop, and so park and start again on the next request: a supervised program,
    never a critical app or a row inside a critical app's program (``stoppable_program_of`` in the shell)."""
    if not row.program or row.critical:
        return False
    return not any(other.critical and other.program == row.program for other in rows)


@pure
def is_app_stoppable(row: RegistryRow, rows: Sequence[RegistryRow]) -> bool:
    """Whether the page offers Stop for the app: what the shell would quit, less the apps this one never stops."""
    return row.name not in NEVER_STOPPED_APP_NAMES and is_quittable_by_shell(row, rows)


@pure
def app_quit_route(name: str) -> str:
    return f"/api/apps/{urllib.parse.quote(name, safe='')}/quit"


def request_app_quit(client: httpx.Client, base_url: str, name: str) -> None:
    """Ask the shell to quit an app; raises ShellUnavailableError with the shell's own reason."""
    url = f"{base_url}{app_quit_route(name)}"
    try:
        response = client.post(url, timeout=APP_QUIT_TIMEOUT_SECONDS)
    except httpx.HTTPError as e:
        raise ShellUnavailableError(f"could not reach the desktop at {url}: {e}") from e
    if response.is_error:
        raise ShellUnavailableError(f"the desktop refused to stop the app: {refusal_reason(response.text)}")

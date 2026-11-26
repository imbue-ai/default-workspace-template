"""The workspace's suggestion switches, read and changed where the app that holds them keeps them.

The two switches (whether suggested replies are offered under an agent's reply, and whether things to try are
offered on a chat with no message yet) are the settings of one of the workspace's apps, which names the path that
reads and changes them in its manifest (``suggestion_settings_path``). The shell passes a client's read and write
there over loopback, so the app keeps the one lock on its settings and tells its own open pages of a change. The
shell names no app: it asks whichever registered app declares the path.

The path answers ``{"settings": {...}}`` holding both switches, to a ``GET`` and to a ``PUT`` whose body names the
switches to change.
"""

from collections.abc import Sequence
from typing import Any
from typing import Final

import httpx
from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import AppInventoryEntry
from imbue.system_interface.shell.errors import SuggestionSettingsUnavailableError
from imbue.system_interface.shell.errors import SuggestionSettingsUnsupportedError

NEXT_STEP_SWITCH: Final[str] = "is_next_step_suggestions_enabled"
NEW_CHAT_SWITCH: Final[str] = "is_new_chat_suggestions_enabled"
SUGGESTION_SWITCHES: Final[tuple[str, ...]] = (NEXT_STEP_SWITCH, NEW_CHAT_SWITCH)

# One loopback request an app answers from a small file; an app that takes longer than this has hung.
SUGGESTION_SETTINGS_TIMEOUT_SECONDS: Final[float] = 10.0
_SETTINGS_FIELD: Final[str] = "settings"
# What an app from before the path answers a request to it with.
_NO_SUCH_ROUTE_STATUSES: Final[frozenset[int]] = frozenset({404, 405})


@pure
def suggestion_settings_url(entries: Sequence[AppInventoryEntry]) -> str:
    """Where the workspace's suggestion switches are read and changed: the registered URL of the first app that
    declares the path, plus the path. Raises ``SuggestionSettingsUnsupportedError`` when no registered app
    declares one, which is a workspace whose app has not registered under a template that has the path."""
    for entry in entries:
        if entry.row.suggestion_settings_path is not None:
            return f"{str(entry.row.url).rstrip('/')}{entry.row.suggestion_settings_path}"
    raise SuggestionSettingsUnsupportedError(
        "No app of this workspace holds suggestion switches the workspace can read and change."
    )


def _switches_answered(response: httpx.Response, url: str) -> dict[str, bool]:
    """Both switches out of the app's answer; raises for an answer that is not the settings holding them."""
    if response.status_code in _NO_SUCH_ROUTE_STATUSES:
        raise SuggestionSettingsUnsupportedError(
            "The app that holds the suggestion switches is from before they could be read and changed here."
        )
    if response.status_code != httpx.codes.OK:
        logger.warning("The suggestion switches at {} answered {}", url, response.status_code)
        raise SuggestionSettingsUnavailableError(
            f"The app that holds the suggestion switches answered {response.status_code}."
        )
    try:
        body: Any = response.json()
    except ValueError as e:
        raise SuggestionSettingsUnavailableError(
            "The app that holds the suggestion switches answered something other than its settings."
        ) from e
    settings = body.get(_SETTINGS_FIELD) if isinstance(body, dict) else None
    switches = {name: settings.get(name) for name in SUGGESTION_SWITCHES} if isinstance(settings, dict) else {}
    if not all(isinstance(switches.get(name), bool) for name in SUGGESTION_SWITCHES):
        raise SuggestionSettingsUnsupportedError(
            "The app that holds the suggestion switches answered settings without them."
        )
    return {name: switches[name] is True for name in SUGGESTION_SWITCHES}


def _exchange(method: str, url: str, changes: dict[str, bool] | None) -> dict[str, bool]:
    try:
        response = httpx.request(method, url, json=changes, timeout=SUGGESTION_SETTINGS_TIMEOUT_SECONDS)
    except httpx.HTTPError as e:
        logger.warning("Could not reach the suggestion switches at {}: {}", url, e)
        raise SuggestionSettingsUnavailableError(
            "The app that holds the suggestion switches is not running, or did not answer."
        ) from e
    return _switches_answered(response, url)


def read_suggestion_switches(entries: Sequence[AppInventoryEntry]) -> dict[str, bool]:
    """Both switches, as the app that holds them answers them."""
    return _exchange("GET", suggestion_settings_url(entries), None)


def change_suggestion_switches(entries: Sequence[AppInventoryEntry], changes: dict[str, bool]) -> dict[str, bool]:
    """Change the switches ``changes`` names, leave the other, and answer both as they then stand."""
    return _exchange("PUT", suggestion_settings_url(entries), changes)

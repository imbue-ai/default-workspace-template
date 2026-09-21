"""Telling an app that a window of its closed (docs/system/specs/window-bound-resources.md section 4.6).

An app whose registry row names a ``window_closed_path`` is posted the closed window from a thread of
its own, with a short timeout, and the answer is never waited on or acted on: the close is complete
whether or not the app is up, and the app reads the shell's desktops for the truth.
"""

import threading
from typing import Any
from typing import Final

import httpx
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import AppInventoryEntry
from imbue.system_interface.shell.data_types import Window
from imbue.system_interface.shell.primitives import DesktopId

HINT_TIMEOUT_SECONDS: Final[float] = 2.0


class WindowClosedHint(FrozenModel):
    """One post: where it goes, and the closed window it carries."""

    app: str = Field(description="The app the window belonged to, for the log")
    url: str = Field(description="The app's registered URL plus its window_closed_path")
    body: dict[str, Any] = Field(description="The closed window: its path, id, and desktop")


@pure
def window_closed_hint(entry: AppInventoryEntry, desktop_id: DesktopId, window: Window) -> WindowClosedHint | None:
    """The hint an app is owed for a closed window of its, or None for an app that declares no path."""
    if entry.row.window_closed_path is None:
        return None
    return WindowClosedHint(
        app=str(entry.row.name),
        url=f"{str(entry.row.url).rstrip('/')}{entry.row.window_closed_path}",
        body={"path": str(window.path), "window_id": str(window.id), "desktop_id": str(desktop_id)},
    )


def _post(hint: WindowClosedHint) -> None:
    try:
        response = httpx.post(hint.url, json=hint.body, timeout=HINT_TIMEOUT_SECONDS)
    except httpx.HTTPError as e:
        logger.debug("Could not tell {} that window {} closed: {}", hint.app, hint.body["window_id"], e)
        return
    if response.is_error:
        logger.debug(
            "Told {} that window {} closed and it answered {}", hint.app, hint.body["window_id"], response.status_code
        )


def post_window_closed_hint(hint: WindowClosedHint) -> None:
    """Post the hint from a daemon thread, so no close waits on an app."""
    threading.Thread(target=_post, args=(hint,), daemon=True, name=f"window-closed-hint-{hint.app}").start()

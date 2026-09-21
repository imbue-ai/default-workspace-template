"""Reading the shell's windows over loopback: what an app with window-bound resources sweeps against
(docs/system/specs/window-bound-resources.md section 4.2)."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure
from loguru import logger

from app_manifest.primitives import AppName

# The shell's address, resolved as system/scripts/layout.py resolves it.
DEFAULT_SHELL_URL: Final[str] = "http://127.0.0.1:8000"
ENV_SHELL_URL: Final[str] = "MINDS_WORKSPACE_SERVER_URL"

# The desktops document (desktop-interface contracts.md section 5.2): every desktop with its windows.
DESKTOPS_ROUTE: Final[str] = "/api/desktops"

# One loopback read of a small file the shell holds in memory; past this it is not answering.
WINDOW_READ_TIMEOUT_SECONDS: Final[float] = 2.0


def shell_base_url() -> str:
    return os.environ.get(ENV_SHELL_URL, DEFAULT_SHELL_URL).rstrip("/")


def read_app_window_paths(shell_url: str, app: AppName) -> list[str] | None:
    """Every window path of ``app`` across every desktop, or None when the shell could not be read.

    None is never "no windows": an app that collects what no window shows must skip a sweep it
    cannot ground in the shell's own answer, so an unreachable shell, a non-JSON body, and a
    document of the wrong shape all read as unknown.
    """
    url = f"{shell_url}{DESKTOPS_ROUTE}"
    try:
        with urllib.request.urlopen(url, timeout=WINDOW_READ_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except (urllib.error.URLError, OSError) as e:
        logger.debug("Could not read the shell's desktops at {}: {}", url, e)
        return None
    try:
        document = json.loads(raw)
    except ValueError as e:
        logger.warning("The shell's desktops at {} are not JSON: {}", url, e)
        return None
    paths = window_paths_of_app(document, app)
    if paths is None:
        logger.warning("The shell's desktops at {} are not shaped as {{desktops: [{{windows: [...]}}]}}", url)
    return paths


@pure
def window_paths_of_app(document: Any, app: AppName) -> list[str] | None:
    """The paths of ``app``'s windows in a desktops document, or None when the document is not one."""
    if not isinstance(document, dict) or not isinstance(document.get("desktops"), list):
        return None
    paths: list[str] = []
    for desktop in document["desktops"]:
        if not isinstance(desktop, dict) or not isinstance(desktop.get("windows"), list):
            return None
        for window in desktop["windows"]:
            if not isinstance(window, dict):
                return None
            window_app = window.get("app")
            path = window.get("path")
            if not isinstance(window_app, str) or not isinstance(path, str):
                return None
            if window_app == app:
                paths.append(path)
    return paths


@pure
def window_query_value(path: str, name: str) -> str | None:
    """The first value of the query parameter ``name`` in a window path, or None when it carries none.

    A settling window still sits at its launch path (``/new?workdir=...``), which names no resource.
    """
    values = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get(name)
    return values[0] if values else None

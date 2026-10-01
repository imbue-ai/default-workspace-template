"""The app registry as this package reads it: each registered app's
``priority`` band name, keyed by the supervisord program that runs it (the
memory backstop), and an app's ``url`` (the memory candidates command, which
finds the browser service there).

``data/.state/apps.toml`` is written by ``system/scripts/forward_port.py``,
which copies ``priority`` and ``program`` from the app's manifest
(``system/apps/<package>/app.toml``; see ``system/libs/app_manifest``). Only
those few keys matter here, so this is a deliberately narrow, stdlib-only
reader rather than the library's full row model: like every module in this
package it is imported under a plain ``python3``.
"""

import logging
import os
import tomllib
from pathlib import Path
from typing import Final

# The registry's location, exactly as forward_port.py and layout.py resolve it:
# relative to the cwd (the repo root under supervisord) unless MINDS_APPS_FILE
# points elsewhere.
DEFAULT_APPS_FILE: Final[str] = "data/.state/apps.toml"
ENV_APPS_FILE: Final[str] = "MINDS_APPS_FILE"

# What a row without ``priority`` (a manifest-less registration, or a manifest
# that omitted it) resolves to: the user-service band's key.
DEFAULT_PRIORITY: Final[str] = "user"

# Diagnostics go through the stdlib logger: with no handler configured (the
# backstop runs under a plain python3), Python's last-resort handler prints
# warnings to stderr, which supervisord captures in the listener's stderr log.
_logger = logging.getLogger(__name__)


def registry_path() -> Path:
    return Path(os.environ.get(ENV_APPS_FILE, DEFAULT_APPS_FILE))


def _read_app_rows(path: Path) -> list[dict]:
    """The registry's ``[[apps]]`` rows.

    A missing registry is empty (nothing has registered yet). An unreadable or
    unparseable one is logged as a warning and also reads as empty.
    """
    if not path.exists():
        return []
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        _logger.warning("Skipped the app registry at %s: it cannot be read (%s)", path, error)
        return []
    apps = doc.get("apps", [])
    if not isinstance(apps, list):
        _logger.warning("Skipped the app registry at %s: it has no [[apps]] array", path)
        return []
    return [app for app in apps if isinstance(app, dict)]


def read_app_url(path: Path, app_name: str) -> str | None:
    """The ``url`` the registry row named ``app_name`` serves at, or None when no row has one."""
    for app in _read_app_rows(path):
        url = app.get("url")
        if app.get("name") == app_name and isinstance(url, str) and url:
            return url.rstrip("/")
    return None


def read_priority_by_program(path: Path) -> dict[str, str]:
    """The ``priority`` of every registry row that names a ``program``, by that program.

    A missing, unreadable or unparseable registry reads as empty, so a corrupt
    file demotes an app to the by-name and user-service fallbacks rather than
    taking the listener down.
    """
    priority_by_program: dict[str, str] = {}
    for app in _read_app_rows(path):
        program = app.get("program")
        if not isinstance(program, str) or not program:
            continue
        priority = app.get("priority", DEFAULT_PRIORITY)
        priority_by_program[program] = priority if isinstance(priority, str) else DEFAULT_PRIORITY
    return priority_by_program

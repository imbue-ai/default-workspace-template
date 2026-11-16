"""Hold a supervisord program back until the workspace's critical path is serving, then exec it.

Usage, as a command prefix in a ``system/supervisord.conf.d/<name>.conf`` (after the
``oom_tag_service.py`` prefix, so the wait already sits in the program's band)::

    command=python3 system/services/oom_priority/bin/oom_tag_service.py files python3 system/scripts/wait_for_critical_path.py .venv/bin/files-app

supervisord spawns every ``autostart`` program at once, and ``priority=`` only orders those
spawns: it never waits for one to be ready before the next. So every cold start shares the CPUs
with the two a new workspace's user waits on: the system interface, which Imbue Studio polls
before it lets the user in, and the chat app, which serves the welcome chat they land on. A
program that waits here starts once both accept connections at the URLs their registry rows
name.

A row that is not yet in the registry (the first boot of a new workspace) is waited for like
one that is not yet listening. Past ``_DEADLINE_SECONDS`` the command starts anyway, with one
line on stderr, so a broken system interface or chat never keeps a backup or a sync from
running. A start once both are up (a restart, a wake by the shell) execs at once.

Nothing is written to stdout: an event listener's stdout carries supervisord's protocol.
"""

import os
import socket
import sys
import time
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from app_registration.forward_port import (
    MalformedRegistryError,
    apps_file,
    load_apps,
)

CRITICAL_PATH_APPS = ("system_interface", "chat")

# Well past a contended cold boot of both apps on a small slice.
_DEADLINE_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 0.25
_CONNECT_TIMEOUT_SECONDS = 0.5


class UnreadableRegistryError(Exception):
    """The registry exists but holds no readable ``[[apps]]`` array."""


def _read_registry_rows(registry: Path) -> list[dict[str, object]]:
    """The registry's ``[[apps]]`` rows; empty while it does not exist yet (a new workspace's first boot)."""
    try:
        return load_apps(registry)
    except FileNotFoundError:
        return []
    except tomllib.TOMLDecodeError as error:
        raise UnreadableRegistryError(
            f"{registry} is not valid TOML: {error}"
        ) from error
    except MalformedRegistryError as error:
        raise UnreadableRegistryError(str(error)) from error
    except (OSError, UnicodeDecodeError) as error:
        raise UnreadableRegistryError(f"cannot read {registry}: {error}") from error


def _registry_problem(registry: Path) -> str | None:
    """Why the registry yields no rows, for the deadline line; None when it reads or does not exist yet."""
    try:
        _read_registry_rows(registry)
    except UnreadableRegistryError as error:
        return str(error)
    return None


def _registered_url_by_name(registry: Path) -> dict[str, str]:
    """Each registry row's ``url`` by its ``name``.

    Empty while the registry is missing or unreadable; the deadline line reports an unreadable one
    (``_registry_problem``) rather than every poll.
    """
    try:
        rows = _read_registry_rows(registry)
    except UnreadableRegistryError:
        return {}
    url_by_name: dict[str, str] = {}
    for row in rows:
        name, url = row.get("name"), row.get("url")
        if isinstance(name, str) and isinstance(url, str):
            url_by_name[name] = url
    return url_by_name


def _accepts_connections(url: str) -> bool:
    """Whether ``url``'s host accepts a TCP connection; a url naming no reachable address never does."""
    try:
        parts = urlsplit(url)
        explicit_port = parts.port
    except ValueError:
        return False
    if parts.hostname is None:
        return False
    port = explicit_port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection(
            (parts.hostname, port), timeout=_CONNECT_TIMEOUT_SECONDS
        ):
            return True
    except OSError:
        return False


def _apps_not_serving(registry: Path, app_names: list[str]) -> list[str]:
    url_by_name = _registered_url_by_name(registry)
    return [
        name
        for name in app_names
        if name not in url_by_name or not _accepts_connections(url_by_name[name])
    ]


def wait_for_apps(
    registry: Path,
    app_names: tuple[str, ...],
    deadline_seconds: float,
    poll_interval_seconds: float,
) -> list[str]:
    """Wait until every named app has accepted a connection, or the deadline passes; the names that never did.

    An app is probed only until it first accepts: every deferred program waits here at once, and each probe
    of an app already serving would cost it a request thread while the other app is still starting.
    """
    deadline = time.monotonic() + deadline_seconds
    waiting_for = list(app_names)
    while True:
        waiting_for = _apps_not_serving(registry, waiting_for)
        if not waiting_for or time.monotonic() >= deadline:
            return waiting_for
        time.sleep(poll_interval_seconds)


def _deadline_message(
    registry: Path, waiting_for: list[str], deadline_seconds: float
) -> str:
    """The stderr line for a start the deadline let through: what was still down, and why when the registry is unreadable."""
    problem = _registry_problem(registry)
    reason = f" ({problem})" if problem is not None else ""
    return (
        f"wait_for_critical_path: {', '.join(waiting_for)} not serving after "
        f"{deadline_seconds:.0f}s{reason}; starting anyway"
    )


def main() -> None:
    command = sys.argv[1:]
    if not command:
        print("usage: wait_for_critical_path.py <command> [args...]", file=sys.stderr)
        sys.exit(2)
    registry = apps_file()
    waiting_for = wait_for_apps(
        registry, CRITICAL_PATH_APPS, _DEADLINE_SECONDS, _POLL_INTERVAL_SECONDS
    )
    if waiting_for:
        print(
            _deadline_message(registry, waiting_for, _DEADLINE_SECONDS),
            file=sys.stderr,
        )
    os.execvp(command[0], command)

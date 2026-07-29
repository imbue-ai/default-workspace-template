#!/usr/bin/env python3
"""Register or remove an app port in data/.state/apps.toml.

Uses file locking to safely upsert or remove entries. Called by services
on startup to declare the ports they expose.

Usage:
    python3 system/scripts/forward_port.py --name terminal --url http://localhost:7681
    python3 system/scripts/forward_port.py --remove --name terminal
"""

import argparse
import fcntl
import os
import re
import tempfile
from pathlib import Path

import tomlkit

DEFAULT_APPS_FILE = "data/.state/apps.toml"
ENV_APPS_FILE = "MINDS_APPS_FILE"

# Service-name rule: lowercase alphanumeric/underscore runs separated by
# single hyphens (no uppercase, no leading/trailing/consecutive hyphens).
# The registered name becomes the first label of the service's origin
# hostname (http://<name>.agent-<hex>.localhost:8421/ locally,
# https://<name>--<host>--<user>.<domain>/ on shares), so it must work as a
# hostname label. Underscores are allowed -- ``system_interface`` predates
# this scheme and underscore labels resolve fine on Cloudflare DNS and in
# Chromium -- but consecutive hyphens would collide with the ``--`` share
# separator. Accepts a superset of KEBAB_RE in the build-app scaffold.
NAME_PATTERN = re.compile(r"^[a-z0-9_]+(?:-[a-z0-9_]+)*$")

# The workspace coordinate in local origins is the ``agent-<hex>`` label; a
# service whose name starts with ``agent-`` would collide with it when the
# forwarder parses hostnames, so the prefix is reserved.
RESERVED_NAME_PREFIX = "agent-"

# ``localhost`` is the local origin's root domain; a service by that name
# would produce the nonsense hostname ``localhost.agent-<hex>.localhost``.
RESERVED_NAMES = frozenset({"localhost"})


def validate_service_name(name: str) -> str | None:
    """Return an error message when ``name`` cannot be a service origin label.

    Returns None for a usable name. Applied to both upsert and remove so a
    bad name fails loudly instead of silently registering an unroutable (or
    unremovable) entry.
    """
    if not NAME_PATTERN.match(name):
        return (
            f"invalid app name {name!r}: names must be lowercase "
            "alphanumeric/underscore runs separated by single hyphens (no "
            "leading, trailing, or consecutive hyphens) because the name "
            "becomes the service's origin hostname label"
        )
    if name.startswith(RESERVED_NAME_PREFIX):
        return (
            f"invalid app name {name!r}: the {RESERVED_NAME_PREFIX!r} prefix is "
            "reserved for the workspace coordinate in service origin hostnames"
        )
    if name in RESERVED_NAMES:
        return f"invalid app name {name!r}: this name is reserved"
    return None


def _apps_file() -> Path:
    """Path to the agent's apps.toml registry.

    Defaults to ``data/.state/apps.toml`` relative to cwd. Override
    via ``MINDS_APPS_FILE`` -- used by tests and by callers that
    need to point at a non-default registry (e.g. when running outside
    the agent's repo root). Mirrors ``system/scripts/layout.py``.
    """
    return Path(os.environ.get(ENV_APPS_FILE, DEFAULT_APPS_FILE))


def _load_apps(path: Path) -> tomlkit.TOMLDocument:
    if not path.exists():
        doc = tomlkit.document()
        doc.add("apps", tomlkit.aot())
        return doc
    with open(path, "rb") as f:
        return tomlkit.load(f)


def _save_apps(path: Path, doc: tomlkit.TOMLDocument) -> None:
    # Atomic write: write to a temp file in the same directory, then os.replace()
    # into place. This guarantees that readers (like app-watcher) never observe
    # a truncated/partial file during the write window.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            tomlkit.dump(doc, f)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _upsert(path: Path, name: str, url: str) -> None:
    doc = _load_apps(path)
    apps = doc.get("apps", [])

    # Find existing entry by name
    for app in apps:
        if app.get("name") == name:
            app["url"] = url
            _save_apps(path, doc)
            return

    # No existing entry -- append
    entry = tomlkit.table()
    entry.add("name", name)
    entry.add("url", url)
    apps.append(entry)
    _save_apps(path, doc)


def _remove(path: Path, name: str) -> None:
    if not path.exists():
        return
    doc = _load_apps(path)
    apps = doc.get("apps", [])
    original_len = len(apps)

    # Remove matching entries
    to_remove = [i for i, app in enumerate(apps) if app.get("name") == name]
    for i in reversed(to_remove):
        del apps[i]

    if len(apps) != original_len:
        _save_apps(path, doc)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register or remove an app port"
    )
    parser.add_argument(
        "--name", required=True, help="App name (e.g. 'terminal', 'browser')"
    )
    parser.add_argument(
        "--url",
        help="Full URL where the app is accessible (e.g. http://localhost:7681)",
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove the named app instead of adding it",
    )
    args = parser.parse_args()

    if not args.remove and not args.url:
        parser.error("--url is required when not using --remove")

    name_error = validate_service_name(args.name)
    if name_error is not None:
        parser.error(name_error)

    apps_file = _apps_file()
    lock_path = apps_file.parent / ".apps.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            if args.remove:
                _remove(apps_file, args.name)
            else:
                _upsert(apps_file, args.name, args.url)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()

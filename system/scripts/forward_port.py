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
import secrets
import tempfile
from pathlib import Path

import tomlkit

DEFAULT_APPS_FILE = "data/.state/apps.toml"
ENV_APPS_FILE = "MINDS_APPS_FILE"

# Each service's public origin uses an UNGUESSABLE hostname label,
# ``<name>-<rand>`` (e.g. ``terminal-x7k9q2w1``), both locally
# (``<label>.host-<hex>.localhost``) and on a share
# (``<label>.<ws-domain>``). On a share the workspace's frpc claims these
# explicit labels instead of the wildcard, so the relay drops any SNI it was
# not told about -- CT only ever exposes the bare ``*.<ws-domain>`` cert name,
# which no longer routes. The random suffix is the one hostname component CT
# never sees. Minted once per service and persisted (stable across re-share so
# bookmarks/layouts keep working); see blueprint/random-service-labels/.
_LABEL_RANDOM_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_LABEL_RANDOM_LENGTH = 8

# Cap the service name so ``<name>-<rand>`` always fits a 63-char DNS label
# (name + 1 hyphen + 8 random chars), with generous headroom.
MAX_SERVICE_NAME_LENGTH = 32

# Service-name rule: lowercase alphanumeric/underscore runs separated by
# single hyphens (no uppercase, no leading/trailing/consecutive hyphens).
# The registered name becomes the leading label of the service's origin
# hostname (http://<name>.host-<hex>.localhost:8421/ locally; share
# hostnames follow the same prefix rule on a longer base), so it must work
# as a hostname label. Underscores are tolerated for legacy names --
# ``system_interface`` predates this scheme and underscore labels resolve
# fine in browsers -- but new apps should stick to kebab-case (the build-app
# scaffold enforces the stricter rule; a drift test in forward_port_test.py
# keeps that rule a subset of this one).
NAME_PATTERN = re.compile(r"^[a-z0-9_]+(?:-[a-z0-9_]+)*$")

# Workspace hostnames carry their coordinate as a ``host-<hex>`` label (and
# ``agent-`` is the legacy spelling of the same coordinate). A service whose
# name starts with either prefix could collide with the coordinate label when
# hostnames are parsed, so both prefixes are reserved.
RESERVED_NAME_PREFIXES = ("host-", "agent-")

# ``localhost`` is the local origin's root domain; a service by that name
# would produce the nonsense hostname ``localhost.host-<hex>.localhost``.
# ``auth`` is reserved for the share stack's dedicated ``auth-<rand>`` label
# (the sole public ``/_auth/*`` origin), so no app may claim it.
RESERVED_NAMES = frozenset({"localhost", "auth"})


def mint_service_label(name: str) -> str:
    """Mint an unguessable ``<name>-<rand>`` origin label for a freshly-registered service."""
    suffix = "".join(secrets.choice(_LABEL_RANDOM_ALPHABET) for _ in range(_LABEL_RANDOM_LENGTH))
    return f"{name}-{suffix}"


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
            "becomes the leading label of the service's origin hostname"
        )
    if len(name) > MAX_SERVICE_NAME_LENGTH:
        return (
            f"invalid app name {name!r}: names must be at most "
            f"{MAX_SERVICE_NAME_LENGTH} characters so the '<name>-<random>' "
            "origin label fits a 63-character DNS label"
        )
    for prefix in RESERVED_NAME_PREFIXES:
        if name.startswith(prefix):
            return (
                f"invalid app name {name!r}: the {prefix!r} prefix is "
                "reserved for the workspace coordinate in service origin "
                "hostnames"
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

    # Update an existing entry's URL in place, minting a label only if one was
    # never assigned (a legacy row, or a row written before labels existed).
    # The label is stable: re-registration must not change a service's origin.
    for app in apps:
        if app.get("name") == name:
            app["url"] = url
            if not app.get("label"):
                app["label"] = mint_service_label(name)
            _save_apps(path, doc)
            return

    # No existing entry -- append with a freshly-minted label.
    entry = tomlkit.table()
    entry.add("name", name)
    entry.add("url", url)
    entry.add("label", mint_service_label(name))
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

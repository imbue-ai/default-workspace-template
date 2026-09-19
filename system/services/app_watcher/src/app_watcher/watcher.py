"""App-registry watcher service.

Watches data/.state/apps.toml for changes. On startup and on every change,
writes service_registered / service_deregistered events to
events/services/events.jsonl so the desktop client can discover available services.

Uses both inotify (when available) and mtime polling (5-second fallback).
"""

import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple
from uuid import uuid4

from imbue.imbue_common.event_envelope import (
    EventEnvelope,
    EventId,
    EventSource,
    EventType,
    IsoTimestamp,
)

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

APPS_FILE = Path("data/.state/apps.toml")
# mtime-polling fallback interval. Kept low (5s) because under the gVisor (runsc)
# runtime, and on the lima/vps providers, file changes made outside the sandbox
# do not raise in-sandbox inotify events -- polling is then the only signal, so a
# tighter interval bounds the worst-case service-discovery latency.
POLL_INTERVAL_SECONDS = 5

_EVENT_SOURCE = EventSource("services")
_EVENT_TYPE_REGISTERED = EventType("service_registered")
_EVENT_TYPE_DEREGISTERED = EventType("service_deregistered")


class ServiceRegisteredEvent(EventEnvelope):
    """A service registered its URL with the agent."""

    service: str
    url: str
    # The service's unguessable origin label (``<name>-<rand>``); consumers
    # route ``<label>.<host>`` origins to this service. Empty for a legacy row
    # written before labels existed (consumers fall back to the service name).
    label: str = ""
    # The app's registered SVG icon markup, verbatim from apps.toml (validated
    # on the way in by forward_port.py). Consumers must sanitize before
    # inlining; empty when the app registered none.
    icon: str = ""


class ServiceDeregisteredEvent(EventEnvelope):
    """A service that was previously registered is no longer available."""

    service: str


class _AppRow(NamedTuple):
    """One app's registered fields: everything a registration event carries but its name."""

    url: str
    label: str
    icon: str


def _new_event_id() -> EventId:
    return EventId(f"evt-{uuid4().hex}")


def _now_iso() -> IsoTimestamp:
    return IsoTimestamp(
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    )


def _get_events_dir() -> Path | None:
    """Get the events directory from MNGR_AGENT_STATE_DIR."""
    state_dir = os.environ.get("MNGR_AGENT_STATE_DIR")
    if not state_dir:
        return None
    return Path(state_dir) / "events" / "services"


def _load_apps() -> list[dict[str, object]]:
    """Load apps from the TOML file."""
    if not APPS_FILE.exists():
        return []
    with open(APPS_FILE, "rb") as f:
        data = tomllib.load(f)
    return data.get("apps", [])


def _registered_rows(current_apps: list[dict[str, object]]) -> dict[str, _AppRow]:
    """The registry's registerable rows, keyed by service name.

    An entry without a name or a URL is not registerable and is dropped here, so
    it can neither be emitted nor counted as present.
    """
    rows: dict[str, _AppRow] = {}
    for app in current_apps:
        name = str(app.get("name", ""))
        url = str(app.get("url", ""))
        if not name or not url:
            continue
        rows[name] = _AppRow(
            url=url, label=str(app.get("label", "")), icon=str(app.get("icon", ""))
        )
    return rows


def _write_events(
    events_dir: Path,
    current_rows: dict[str, _AppRow],
    previous_rows: dict[str, _AppRow],
) -> None:
    """Write a registration event per app whose row changed, and a deregistration per app that left.

    Only *changed* rows are emitted. The registry file is rewritten in full every
    time any app registers (``forward_port.py`` replaces it atomically), so a
    write says nothing about which apps changed, and an app that restarts in a
    loop would otherwise re-announce every app in the file on every restart.
    """
    events_dir.mkdir(parents=True, exist_ok=True)
    events_path = events_dir / "events.jsonl"

    with open(events_path, "a") as f:
        for name, row in current_rows.items():
            if previous_rows.get(name) == row:
                continue
            event = ServiceRegisteredEvent(
                timestamp=_now_iso(),
                type=_EVENT_TYPE_REGISTERED,
                event_id=_new_event_id(),
                source=_EVENT_SOURCE,
                service=name,
                url=row.url,
                label=row.label,
                icon=row.icon,
            )
            f.write(event.model_dump_json() + "\n")

        for name in sorted(set(previous_rows) - set(current_rows)):
            event = ServiceDeregisteredEvent(
                timestamp=_now_iso(),
                type=_EVENT_TYPE_DEREGISTERED,
                event_id=_new_event_id(),
                source=_EVENT_SOURCE,
                service=name,
            )
            f.write(event.model_dump_json() + "\n")


def _try_setup_inotify(path: Path) -> object | None:
    """Try to set up inotify on the apps file's parent directory.

    Uses inotify_simple (pure Python, Linux only). Returns the INotify
    instance on success, or None on non-Linux platforms or errors.
    """
    try:
        from inotify_simple import INotify
        from inotify_simple import flags as inotify_flags

        inotify = INotify()
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        inotify.add_watch(
            str(parent),
            inotify_flags.MODIFY | inotify_flags.CREATE | inotify_flags.MOVED_TO,
        )
        return inotify
    except (ImportError, OSError):
        return None


def _wait_for_change_inotify(inotify: object, timeout_seconds: float) -> bool:
    """Wait for an inotify event, with timeout."""
    try:
        from inotify_simple import INotify

        if not isinstance(inotify, INotify):
            return False
        timeout_ms = int(timeout_seconds * 1000)
        events = inotify.read(timeout=timeout_ms)
        return len(events) > 0
    except (ImportError, OSError):
        return False


def main() -> None:
    """Main loop: watch apps.toml and write service events."""
    print("[app-watcher] Starting app watcher", file=sys.stderr, flush=True)

    APPS_FILE.parent.mkdir(parents=True, exist_ok=True)

    events_dir = _get_events_dir()
    if events_dir is None:
        print(
            "[app-watcher] WARNING: MNGR_AGENT_STATE_DIR not set, events will not be written",
            file=sys.stderr,
            flush=True,
        )

    inotify_fd = _try_setup_inotify(APPS_FILE)
    if inotify_fd is not None:
        print(
            "[app-watcher] Using inotify for file watching", file=sys.stderr, flush=True
        )
    else:
        print(
            "[app-watcher] inotify not available, using polling only",
            file=sys.stderr,
            flush=True,
        )

    last_mtime: float = 0.0
    previous_rows: dict[str, _AppRow] = {}

    def _handle_signal(signum: int, frame: object) -> None:
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    while True:
        try:
            new_mtime = (
                APPS_FILE.stat().st_mtime if APPS_FILE.exists() else 0.0
            )
        except OSError:
            new_mtime = 0.0

        if new_mtime != last_mtime:
            last_mtime = new_mtime
            current_rows = _registered_rows(_load_apps())
            changed = [
                name
                for name, row in current_rows.items()
                if previous_rows.get(name) != row
            ]
            gone = sorted(set(previous_rows) - set(current_rows))

            if changed or gone:
                print(
                    f"[app-watcher] Apps changed: registered={changed} deregistered={gone}",
                    file=sys.stderr,
                    flush=True,
                )

            # Write service events
            if events_dir is not None:
                _write_events(events_dir, current_rows, previous_rows)

            # Track current rows for next diff
            previous_rows = current_rows

        # Wait for changes
        if inotify_fd is not None:
            _wait_for_change_inotify(inotify_fd, POLL_INTERVAL_SECONDS)
        else:
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

"""`host-backup-now` CLI.

Convenience wrapper for forcing an immediate backup tick. The script's
mtime-polling loop already reacts to config-file edits, so all this CLI
does is touch backup.toml -- except when a backup is currently running,
in which case it waits for the in-flight one to finish first (so the
user's most recent edits are guaranteed to land in the next tick rather
than the in-flight one).

It then waits for the triggered tick to reach any terminal event and prints it,
exiting 0 on success, 3 when backups are not configured, 1 on any other tick
outcome, and 2 when no outcome was observed at all (no terminal event before the
timeout, or no events log to read in the first place). When the in-flight tick is
still running at the timeout, it exits 2 without triggering a tick.

With `--check` it triggers nothing and waits for nothing: it reads the newest tick
outcomes back from the log, prints them, and exits 0 when a `restic_backup_succeeded`
is recent (within two backup intervals), 3 when the newest tick ended for missing
secrets, and 1 otherwise (the service is down, or its ticks fail).
"""

import json
import os
import sys
import time
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Final

import click
from loguru import logger

from host_backup.config import (
    BACKUP_TOML_PATH,
    load_backup_config,
    resolve_service_events_dir,
)
from host_backup.events import (
    BACKUP_EVENT_SOURCE,
    EVENTS_FILENAME,
    TICK_TERMINAL_EVENT_TYPES,
    BackupEventType,
    scan_recent_ticks,
    scan_recent_ticks_across_rotation,
)

DEFAULT_TIMEOUT_SECONDS = 1800.0  # 30 minutes
_POLL_INTERVAL_SECONDS = 0.5

# Exit codes. "Backups are not configured" is a distinct outcome from "the
# backup attempt failed": callers that take a backup as a precondition (e.g. the
# update-self skill) have to tell "there is no restore point" from "something
# broke" without parsing the printed event.
EXIT_BACKUP_SUCCEEDED: Final[int] = 0
EXIT_BACKUP_FAILED: Final[int] = 1
EXIT_NO_COMPLETION_OBSERVED: Final[int] = 2
EXIT_BACKUPS_NOT_CONFIGURED: Final[int] = 3


@click.command()
@click.option(
    "--timeout",
    "timeout_seconds",
    default=DEFAULT_TIMEOUT_SECONDS,
    show_default=True,
    help="How long (seconds) to wait in all: for a tick already in flight, then for the triggered one",
)
@click.option(
    "--check",
    is_flag=True,
    help="Only report whether a recent backup succeeded; trigger and wait for nothing",
)
def backup_now_main(timeout_seconds: float, check: bool) -> None:
    """Trigger an immediate host_backup tick and wait for it to complete."""
    events_dir = resolve_service_events_dir()
    if events_dir is None:
        logger.error(
            "Cannot locate the host-backup events log: the service has published "
            "no events-dir pointer and MNGR_AGENT_STATE_DIR is unset"
        )
        sys.exit(EXIT_NO_COMPLETION_OBSERVED)
    events_path = events_dir / EVENTS_FILENAME
    if check:
        sys.exit(_check_recent_backup(events_path))

    deadline = time.monotonic() + timeout_seconds
    # Opened before the scan, so a tick that ends between the scan and the first poll
    # is still seen ending.
    with closing(_EventsLogFollower(events_path)) as follower:
        inflight_tick_id = scan_recent_ticks(events_path).inflight_tick_id
        is_idle = inflight_tick_id is None or _wait_for_tick_to_end(
            follower, inflight_tick_id, deadline
        )
    if not is_idle:
        logger.error(
            "Timed out waiting for the in-flight backup tick to finish; triggered nothing"
        )
        sys.exit(EXIT_NO_COMPLETION_OBSERVED)
    # Opened before the bump, so every event of the triggered tick lands after it.
    with closing(_EventsLogFollower(events_path)) as follower:
        _bump_config_mtime()
        completion = _wait_for_next_completion(follower, deadline)
    if completion is None:
        logger.error("Timed out waiting for backup to complete")
        sys.exit(EXIT_NO_COMPLETION_OBSERVED)
    click.echo(json.dumps(completion, default=str))
    sys.exit(_exit_code_for_completion(completion))


def _check_recent_backup(events_path: Path) -> int:
    """Print the newest tick outcomes and map them to an exit code: 0 when the newest
    `restic_backup_succeeded` is within two backup intervals, 3 when the newest tick
    ended for missing secrets, 1 otherwise.

    Two intervals: a healthy service starts the next tick one interval after the
    previous one ended, so its newest success is at most one interval plus one run
    old, and a run longer than the interval is the slow-backup notice's business.
    """
    recent = scan_recent_ticks_across_rotation(events_path)
    max_age_seconds = 2 * load_backup_config().backup_interval_seconds
    success_at = _event_time(recent.newest_success_event)
    age_seconds = (
        None
        if success_at is None
        else (datetime.now(timezone.utc) - success_at).total_seconds()
    )
    newest_outcome = (
        None
        if recent.newest_terminal_event is None
        else recent.newest_terminal_event.get("type")
    )
    click.echo(
        json.dumps(
            {
                "newest_outcome": newest_outcome,
                "newest_success_at": None
                if success_at is None
                else success_at.isoformat(),
                "age_seconds": age_seconds,
                "max_age_seconds": max_age_seconds,
                "inflight_tick_id": recent.inflight_tick_id,
            }
        )
    )
    if newest_outcome == BackupEventType.TICK_SKIPPED_DUE_TO_MISSING_SECRETS.value:
        return EXIT_BACKUPS_NOT_CONFIGURED
    if age_seconds is not None and age_seconds <= max_age_seconds:
        return EXIT_BACKUP_SUCCEEDED
    return EXIT_BACKUP_FAILED


def _event_time(event: dict[str, object] | None) -> datetime | None:
    if event is None or not isinstance(timestamp := event.get("timestamp"), str):
        return None
    try:
        return datetime.fromisoformat(timestamp)
    except ValueError:
        return None


def _exit_code_for_completion(completion: dict[str, object]) -> int:
    """Map the terminal event that ended the tick to this command's exit code."""
    event_type = completion.get("type")
    if event_type == BackupEventType.RESTIC_BACKUP_SUCCEEDED.value:
        return EXIT_BACKUP_SUCCEEDED
    if event_type == BackupEventType.TICK_SKIPPED_DUE_TO_MISSING_SECRETS.value:
        return EXIT_BACKUPS_NOT_CONFIGURED
    return EXIT_BACKUP_FAILED


def _bump_config_mtime() -> None:
    """Update backup.toml's mtime so the runner's poll loop kicks off a new tick."""
    if not BACKUP_TOML_PATH.exists():
        # No config yet; create an empty file rather than skipping so the
        # runner still sees an mtime change. Tolerant loading reads an empty
        # backup.toml as an all-defaults config.
        BACKUP_TOML_PATH.parent.mkdir(parents=True, exist_ok=True)
        BACKUP_TOML_PATH.touch()
        return
    now = time.time()
    os.utime(BACKUP_TOML_PATH, (now, now))


class _EventsLogFollower:
    """Reads the events appended to the log after it was opened, across a rotation.

    The runner rotates by renaming the log and letting the next event start a fresh
    file, so a byte offset taken on the path would point into a different file
    afterwards. This holds the file it opened instead: once the path names another
    file, it drains the held one and reads the new one from its start.
    """

    def __init__(self, events_path: Path) -> None:
        self._events_path = events_path
        # Bytes after the last complete line: an event still being appended.
        self._partial_line = b""
        self._handle = _open_or_none(events_path)
        if self._handle is not None:
            self._handle.seek(0, os.SEEK_END)

    def read_new_events(self) -> list[dict[str, object]]:
        if self._handle is None:
            # There was no log when this began, so all of one that appears is new.
            self._handle = _open_or_none(self._events_path)
        held = self._handle
        if held is None:
            return []
        events = self._read_to_end(held)
        if not _is_path_rotated_away_from(self._events_path, held):
            return events
        events.extend(self._read_to_end(held))
        held.close()
        self._partial_line = b""
        self._handle = _open_or_none(self._events_path)
        if self._handle is not None:
            events.extend(self._read_to_end(self._handle))
        return events

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()

    def _read_to_end(self, handle: BinaryIO) -> list[dict[str, object]]:
        blob = self._partial_line + handle.read()
        *complete_lines, self._partial_line = blob.split(b"\n")
        return _parse_event_lines(complete_lines)


def _open_or_none(events_path: Path) -> BinaryIO | None:
    try:
        return events_path.open("rb")
    except FileNotFoundError:
        return None


def _is_path_rotated_away_from(events_path: Path, held: BinaryIO) -> bool:
    try:
        on_path = events_path.stat()
    except FileNotFoundError:
        # Renamed away, and nothing has started the next file yet.
        return False
    held_stat = os.fstat(held.fileno())
    return (on_path.st_dev, on_path.st_ino) != (held_stat.st_dev, held_stat.st_ino)


def _wait_for_tick_to_end(
    follower: _EventsLogFollower,
    inflight_tick_id: str,
    deadline: float,
) -> bool:
    """Block until the tick in flight emits a terminal event, or the deadline passes.
    Returns False when the deadline passed first.

    A tick that starts meanwhile becomes the one waited for: the runner runs one tick
    at a time, so `inflight_tick_id` died (its runner was shed or restarted) and will
    never end.
    """
    logger.info("Waiting for the in-flight backup tick to complete...")
    while time.monotonic() < deadline:
        for event in follower.read_new_events():
            if event.get("source") != BACKUP_EVENT_SOURCE:
                continue
            tick_id = event.get("tick_id")
            if not isinstance(tick_id, str):
                continue
            event_type = event.get("type")
            if event_type == BackupEventType.BACKUP_STARTED.value:
                inflight_tick_id = tick_id
            elif (
                tick_id == inflight_tick_id and event_type in TICK_TERMINAL_EVENT_TYPES
            ):
                return True
        time.sleep(_POLL_INTERVAL_SECONDS)
    return False


def _wait_for_next_completion(
    follower: _EventsLogFollower,
    deadline: float,
) -> dict[str, object] | None:
    """Block until a terminal event arrives after `follower` was opened, or the
    deadline passes."""
    while time.monotonic() < deadline:
        for event in follower.read_new_events():
            # An abandoned tick is one the service restarted out of; the backup it was
            # running starts over as the restarted service's first tick.
            if event.get("type") == BackupEventType.TICK_ABANDONED.value:
                continue
            if event.get("type") in TICK_TERMINAL_EVENT_TYPES:
                return event
        time.sleep(_POLL_INTERVAL_SECONDS)
    return None


def _parse_event_lines(raw_lines: Sequence[bytes]) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for raw_line in raw_lines:
        line = raw_line.decode(errors="replace")
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


if __name__ == "__main__":
    backup_now_main()

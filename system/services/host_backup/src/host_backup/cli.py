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
timeout, or no events log to read in the first place).
"""

import json
import os
import sys
import time
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import BinaryIO, Final

import click
from loguru import logger

from host_backup.config import BACKUP_TOML_PATH, resolve_service_events_dir
from host_backup.events import (
    BACKUP_EVENT_SOURCE,
    EVENTS_FILENAME,
    EVENTS_LOG_ROTATION_BYTES,
    TICK_TERMINAL_EVENT_TYPES,
    BackupEventType,
)

DEFAULT_TIMEOUT_SECONDS = 1800.0  # 30 minutes
_POLL_INTERVAL_SECONDS = 0.5

# How much of the end of the events log the in-flight scan may read. Comfortably
# more than the events of one tick, and small enough that this command cannot be
# the reason the workspace runs out of memory.
_TAIL_READ_MAX_BYTES = EVENTS_LOG_ROTATION_BYTES

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
    help="How long (seconds) to wait for the triggered backup to finish",
)
def backup_now_main(timeout_seconds: float) -> None:
    """Trigger an immediate host_backup tick and wait for it to complete."""
    events_dir = resolve_service_events_dir()
    if events_dir is None:
        logger.error(
            "Cannot locate the host-backup events log: the service has published "
            "no events-dir pointer and MNGR_AGENT_STATE_DIR is unset"
        )
        sys.exit(EXIT_NO_COMPLETION_OBSERVED)
    events_path = events_dir / EVENTS_FILENAME

    deadline = time.monotonic() + timeout_seconds
    with closing(_EventsLogFollower(events_path)) as follower:
        _wait_for_no_inflight_backup(events_path, follower, deadline)
    # Opened before the bump, so every event of the triggered tick lands after it.
    with closing(_EventsLogFollower(events_path)) as follower:
        _bump_config_mtime()
        completion = _wait_for_next_completion(follower, deadline)
    if completion is None:
        logger.error("Timed out waiting for backup to complete")
        sys.exit(EXIT_NO_COMPLETION_OBSERVED)
    click.echo(json.dumps(completion, default=str))
    sys.exit(_exit_code_for_completion(completion))


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


def _wait_for_no_inflight_backup(
    events_path: Path,
    follower: _EventsLogFollower,
    deadline: float,
) -> None:
    """Block until every tick in flight when this began emits a terminal event, or
    the deadline passes.

    `follower` has to be opened before this scans the log, so a tick that ends
    between the scan and the first poll is still seen ending.
    """
    pending_tick_ids = _scan_for_inflight_tick_ids(events_path, max_lines=200)
    if not pending_tick_ids:
        return
    logger.info(
        "Waiting for {} in-flight backup tick(s) to complete...", len(pending_tick_ids)
    )
    while pending_tick_ids:
        if time.monotonic() >= deadline:
            return
        for event in follower.read_new_events():
            tick_id = event.get("tick_id")
            if (
                isinstance(tick_id, str)
                and event.get("type") in TICK_TERMINAL_EVENT_TYPES
            ):
                pending_tick_ids.discard(tick_id)
        time.sleep(_POLL_INTERVAL_SECONDS)


def _wait_for_next_completion(
    follower: _EventsLogFollower,
    deadline: float,
) -> dict[str, object] | None:
    """Block until a terminal event arrives after `follower` was opened, or the
    deadline passes."""
    while time.monotonic() < deadline:
        for event in follower.read_new_events():
            if event.get("type") in TICK_TERMINAL_EVENT_TYPES:
                return event
        time.sleep(_POLL_INTERVAL_SECONDS)
    return None


def _read_tail_lines(events_path: Path, *, max_lines: int, max_bytes: int) -> list[str]:
    """The last `max_lines` lines of `events_path`, reading at most `max_bytes` from its end.

    Reads `max_bytes` at most, however large the file is. A log written before the
    runner rotated it and capped each event's fields runs to gigabytes on an old
    workspace, one line to hundreds of kilobytes -- reading it whole is what got this
    command killed by the OOM watchdog before it did anything at all.

    The byte ceiling binds first on such a workspace, yielding fewer than `max_lines`
    events. That is the right trade for the one question asked of this: only a tick
    whose BACKUP_STARTED has no completion after it matters, and the events a tick
    emits before it completes are the small ones (the large ones all report a finished
    restic command), so an in-flight tick is always inside the window.
    """
    try:
        with events_path.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - max_bytes))
            blob = fh.read()
    except OSError:
        return []
    lines = blob.decode(errors="replace").splitlines()
    # A window that started mid-file almost certainly cut its first line in half.
    if size > max_bytes and lines:
        lines = lines[1:]
    return lines[-max_lines:]


def _scan_for_inflight_tick_ids(
    events_path: Path, *, max_lines: int, max_bytes: int = _TAIL_READ_MAX_BYTES
) -> set[str]:
    """Return the tick_ids that started but did not finish among the last `max_lines`
    events that fit in the final `max_bytes` of the log."""
    if not events_path.exists():
        return set()
    lines = _read_tail_lines(events_path, max_lines=max_lines, max_bytes=max_bytes)
    started: set[str] = set()
    finished: set[str] = set()
    for raw in lines:
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("source") != BACKUP_EVENT_SOURCE:
            continue
        tick_id = event.get("tick_id")
        if not isinstance(tick_id, str):
            continue
        event_type = event.get("type")
        if event_type == BackupEventType.BACKUP_STARTED.value:
            started.add(tick_id)
        elif event_type in TICK_TERMINAL_EVENT_TYPES:
            finished.add(tick_id)
    return started - finished


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

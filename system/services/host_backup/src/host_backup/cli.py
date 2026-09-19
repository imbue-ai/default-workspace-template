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
from pathlib import Path
from typing import Final

import click
from loguru import logger

from host_backup.config import BACKUP_TOML_PATH, resolve_service_events_dir
from host_backup.events import (
    BACKUP_EVENT_SOURCE,
    TICK_TERMINAL_EVENT_TYPES,
    BackupEventType,
)

DEFAULT_TIMEOUT_SECONDS = 1800.0  # 30 minutes
_POLL_INTERVAL_SECONDS = 0.5

# How much of the end of the events log the in-flight scan may read. Comfortably
# more than the events of one tick, and small enough that this command cannot be
# the reason the workspace runs out of memory.
_TAIL_READ_MAX_BYTES = 8 * 1024 * 1024

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
    events_path = events_dir / "events.jsonl"

    deadline = time.monotonic() + timeout_seconds
    initial_size = _safe_file_size(events_path)

    _wait_for_no_inflight_backup(events_path, initial_size, deadline)
    _bump_config_mtime()
    completion = _wait_for_next_completion(
        events_path, _safe_file_size(events_path), deadline
    )
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


def _safe_file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


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


def _wait_for_no_inflight_backup(
    events_path: Path,
    initial_size: int,
    deadline: float,
) -> None:
    """Block until any in-flight backup has emitted a completion event.

    Reads the existing tail of the events file (before bumping mtime) to
    decide whether a backup is currently in flight, by walking events in
    reverse chronological order until we find either a started-without-
    completion (in flight) or a completion (we're idle).
    """
    pending_tick_ids = _scan_for_inflight_tick_ids(events_path, max_lines=200)
    if not pending_tick_ids:
        return
    logger.info(
        "Waiting for {} in-flight backup tick(s) to complete...", len(pending_tick_ids)
    )
    last_size = initial_size
    while pending_tick_ids:
        if time.monotonic() >= deadline:
            return
        new_size = _safe_file_size(events_path)
        if new_size > last_size:
            for event in _read_new_events(events_path, last_size, new_size):
                tick_id = event.get("tick_id")
                if (
                    isinstance(tick_id, str)
                    and event.get("type") in TICK_TERMINAL_EVENT_TYPES
                ):
                    pending_tick_ids.discard(tick_id)
            last_size = new_size
        time.sleep(_POLL_INTERVAL_SECONDS)


def _wait_for_next_completion(
    events_path: Path,
    initial_size: int,
    deadline: float,
) -> dict[str, object] | None:
    """Block until the triggered tick emits a terminal event, or the deadline passes."""
    last_size = initial_size
    while time.monotonic() < deadline:
        new_size = _safe_file_size(events_path)
        if new_size > last_size:
            for event in _read_new_events(events_path, last_size, new_size):
                if event.get("type") in TICK_TERMINAL_EVENT_TYPES:
                    return event
            last_size = new_size
        time.sleep(_POLL_INTERVAL_SECONDS)
    return None


def _read_tail_lines(events_path: Path, *, max_lines: int, max_bytes: int) -> list[str]:
    """The last `max_lines` lines of `events_path`, reading at most `max_bytes` from its end.

    Reads `max_bytes` at most, however large the file is. Backup events embed the full
    stdout of the restic command they report, so a single line runs to hundreds of
    kilobytes and the log reaches gigabytes on an old workspace -- reading it whole is
    what got this command killed by the OOM watchdog before it did anything at all.

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
    """Look at the last `max_lines` events; return the set of tick_ids that started but did not finish."""
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


def _read_new_events(
    events_path: Path, last_size: int, new_size: int
) -> list[dict[str, object]]:
    """Read events appended between byte offsets last_size and new_size."""
    try:
        with events_path.open("rb") as fh:
            fh.seek(last_size)
            blob = fh.read(new_size - last_size)
    except OSError:
        return []
    events: list[dict[str, object]] = []
    for line in blob.decode(errors="replace").splitlines():
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

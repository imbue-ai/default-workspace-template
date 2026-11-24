"""Where the app keeps what it knows: settings, the watch state, the history, and the measurements.

Everything lives under ``DATA_DIR`` (``data/.apps/disk-space/`` unless ``DISK_SPACE_DATA_DIR`` says
otherwise), so a throwaway copy of the app can run against a copy of it.

- ``settings.json``: the alert line and how often to nudge again while the disk stays over it.
- ``watch.json``: what the last check saw, whether the alert is armed, and the cleanup helper's chat.
- ``history.jsonl``: one line per alert, deletion and helper start, each with everything it touched. The newest
  ``HISTORY_KEPT`` are kept; older lines are dropped as new ones are written.
- ``snapshots/<id>/``: each measurement, the tree it produced (``snapshot.json``) beside the raw
  ``du`` output it was built from (``du-<group>.txt``). The newest few are kept, and the folder carries a
  ``.nobackup`` marker: a measurement can be taken again, so the workspace's backup skips it.
"""

import fcntl
import json
import os
import shutil
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

# Relative paths are taken from the workspace root, so the command finds its data from any folder.
DATA_DIR: Final[Path] = Path("/home/user/workspace") / os.environ.get(
    "DISK_SPACE_DATA_DIR", "data/.apps/disk-space"
)
DEFAULT_THRESHOLD: Final[int] = 90
DEFAULT_RENOTIFY_HOURS: Final[float] = 6.0
SNAPSHOTS_KEPT: Final[int] = 5
HISTORY_KEPT: Final[int] = 500
HISTORY_SHOWN: Final[int] = 30


def data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2))
    temporary.replace(path)


def read_settings() -> dict[str, Any]:
    stored = _read_json(data_dir() / "settings.json")
    threshold = stored.get("threshold", DEFAULT_THRESHOLD)
    renotify = stored.get("renotify_hours", DEFAULT_RENOTIFY_HOURS)
    return {
        "threshold": int(threshold)
        if isinstance(threshold, (int, float))
        else DEFAULT_THRESHOLD,
        "renotify_hours": float(renotify)
        if isinstance(renotify, (int, float))
        else DEFAULT_RENOTIFY_HOURS,
    }


def write_settings(threshold: int) -> dict[str, Any]:
    settings = read_settings()
    settings["threshold"] = max(50, min(99, int(threshold)))
    _write_json(data_dir() / "settings.json", settings)
    return settings


def read_watch() -> dict[str, Any]:
    watch = _read_json(data_dir() / "watch.json")
    watch.setdefault("armed", True)
    return watch


def update_watch(**changes: Any) -> dict[str, Any]:
    watch = read_watch()
    watch.update(changes)
    _write_json(data_dir() / "watch.json", watch)
    return watch


def append_history(kind: str, summary: str, **details: Any) -> dict[str, Any]:
    entry = {
        "id": uuid.uuid4().hex[:12],
        "at": time.time(),
        "kind": kind,
        "summary": summary,
        **details,
    }
    path = data_dir() / "history.jsonl"
    with path.open("a") as handle:
        handle.write(json.dumps(entry) + "\n")
    _bound_history(path)
    return entry


def _bound_history(path: Path) -> None:
    """Drops the oldest lines once the history is past ``HISTORY_KEPT``, so the file cannot grow without limit."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    if len(lines) <= HISTORY_KEPT:
        return
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text("\n".join(lines[-HISTORY_KEPT:]) + "\n")
    temporary.replace(path)


def read_history() -> list[dict[str, Any]]:
    path = data_dir() / "history.jsonl"
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            entries.append(value)
    entries.reverse()
    return entries


def find_history(entry_id: str) -> dict[str, Any] | None:
    return next(
        (entry for entry in read_history() if entry.get("id") == entry_id), None
    )


def snapshots_dir() -> Path:
    """Where the measurements live, marked rebuildable: a restore refills it by measuring the disk again."""
    path = data_dir() / "snapshots"
    path.mkdir(parents=True, exist_ok=True)
    (path / ".nobackup").touch()
    return path


def new_snapshot_dir() -> Path:
    snapshot_id = (
        time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:6]
    )
    path = snapshots_dir() / snapshot_id
    path.mkdir(parents=True)
    return path


def save_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    _write_json(path / "snapshot.json", snapshot)
    _write_json(data_dir() / "latest.json", {"id": path.name})
    _prune_snapshots()


def _prune_snapshots() -> None:
    """Keeps the newest ``SNAPSHOTS_KEPT`` measurements, by when each was taken; a half-written one goes first."""
    measured: list[tuple[float, Path]] = []
    for entry in snapshots_dir().iterdir():
        if not entry.is_dir():
            continue
        taken_at = _read_json(entry / "snapshot.json").get("taken_at")
        measured.append(
            (float(taken_at) if isinstance(taken_at, (int, float)) else 0.0, entry)
        )
    measured.sort()
    for _, old in measured[:-SNAPSHOTS_KEPT]:
        shutil.rmtree(old, ignore_errors=True)


def latest_snapshot() -> dict[str, Any] | None:
    latest = _read_json(data_dir() / "latest.json").get("id")
    if not isinstance(latest, str):
        return None
    return read_snapshot(latest)


def read_snapshot(snapshot_id: str) -> dict[str, Any] | None:
    if "/" in snapshot_id or snapshot_id.startswith("."):
        return None
    snapshot = _read_json(snapshots_dir() / snapshot_id / "snapshot.json")
    return snapshot or None


def rewrite_latest_snapshot(snapshot: dict[str, Any]) -> None:
    _write_json(snapshots_dir() / str(snapshot["id"]) / "snapshot.json", snapshot)


@contextmanager
def held_lock(name: str, wait: bool) -> Generator[bool]:
    """Holds ``<name>.lock`` for the block; yields False (and holds nothing) when ``wait`` is off and it is taken."""
    handle = (data_dir() / f"{name}.lock").open("a")
    try:
        try:
            fcntl.flock(
                handle, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB
            )
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        handle.close()


def is_locked(name: str) -> bool:
    with held_lock(name, wait=False) as acquired:
        return not acquired

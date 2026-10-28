"""What is on the disk, measured only when the user asks (``docs/system/specs/system-monitor.md`` section 4.4).

Walking the disk is slow under gVisor and the figures go stale at once, so nothing here runs on a timer:
``DiskScanner.start`` runs one walk on a thread of the app, the result is kept with its time until the app
stops, and the page says when it was taken. Each row is one ``du`` (GNU: the container is Linux) whose
per-path lines are also the folders the row lists; the app directories are one each and the system
software behind the last row one more; the rest of the disk's used space is that last row.
"""

import subprocess
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, NamedTuple

WORKSPACE_DIR: Final[Path] = Path("/home/user/workspace")
HOME_DIR: Final[Path] = Path("/home/user")
ROOT_DIR: Final[Path] = Path("/")
APP_DATA_DIR: Final[Path] = Path("data/.apps")
UPLOADS_DIR: Final[Path] = Path("data/uploads")
CACHE_DIRS: Final[tuple[Path, ...]] = (
    Path(".cache"),
    Path(".npm"),
    Path(".local/share/uv"),
    Path(".local/share/pnpm"),
)
# What the top-level folders of the workspace hold, for the ones a user would not know.
WORKSPACE_FOLDER_HINTS: Final[dict[str, str]] = {
    ".venv": "installed packages, rebuilt on demand",
    ".git": "the history of changes",
    "system": "the workspace's own code",
    "data": "documents, notes, settings",
    ".agents": "skills",
    "docs": "the workspace's own documentation",
    "catalog": "the app and skill catalog",
}
CACHE_HINTS: Final[dict[str, str]] = {
    ".cache": "downloads and build caches",
    ".npm": "Node packages",
    ".local/share/uv": "Python packages",
    ".local/share/pnpm": "Node packages",
}
# How many of the workspace's folders the row lists, and the least a listed item holds.
WORKSPACE_ITEM_COUNT: Final[int] = 8
MIN_ITEM_BYTES: Final[int] = 1024 * 1024
# What is not the machine's system software: the home tree (the rows above), kernel and memory
# filesystems, and the container engines' own stores where a machine keeps them on this disk.
SYSTEM_EXCLUDES: Final[tuple[Path, ...]] = (
    Path("/proc"),
    Path("/sys"),
    Path("/dev"),
    Path("/run"),
    Path("/tmp"),
    Path("/mnt"),
    Path("/mngr-vol"),
    Path("/var/lib/docker"),
    Path("/var/lib/containerd"),
)
DU_TIMEOUT_SECONDS: Final[float] = 120.0
KIB: Final[int] = 1024


class Measured(NamedTuple):
    """What one ``du`` answered: the paths together, and each on its own."""

    total: int
    by_path: dict[Path, int]


# Measures a set of paths less anything under the excludes; None when they could not be measured.
Measure = Callable[[list[Path], list[Path]], Measured | None]


def du_bytes(paths: list[Path], excludes: list[Path]) -> Measured | None:
    """The size of ``paths`` together and each on its own, less anything under ``excludes``, from one ``du``;
    None when it fails."""
    existing = [path for path in paths if path.exists()]
    if not existing:
        return Measured(0, {})
    command = [
        "du",
        "-sk",
        "--total",
        *(f"--exclude={exclude}" for exclude in excludes),
        *map(str, existing),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=DU_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # du exits 1 when something could not be read but still prints every total it could measure.
    total: int | None = None
    by_path: dict[Path, int] = {}
    for line in result.stdout.splitlines():
        size_text, _, label = line.partition("\t")
        if not size_text.isdigit():
            continue
        if label == "total":
            total = int(size_text) * KIB
        else:
            by_path[Path(label)] = int(size_text) * KIB
    return Measured(total, by_path) if total is not None else None


def _items(
    measured: Measured | None,
    hints: dict[str, str],
    name_of: Callable[[Path], str],
    limit: int | None,
) -> list[dict[str, Any]]:
    """The measured paths as a row's items, largest first, leaving out those under a megabyte."""
    if measured is None:
        return []
    items = [
        {"name": name_of(path), "hint": hints.get(name_of(path), ""), "bytes": size}
        for path, size in measured.by_path.items()
        if size >= MIN_ITEM_BYTES
    ]
    items.sort(key=lambda item: item["bytes"], reverse=True)
    return items[:limit] if limit is not None else items


def scan(
    disk_used: int,
    workspace: Path = WORKSPACE_DIR,
    home: Path = HOME_DIR,
    measure: Measure = du_bytes,
    root: Path = ROOT_DIR,
) -> tuple[list[dict[str, Any]], list[str]]:
    """The rows of what is on the disk, and the ids of the rows that could not be measured."""
    app_data, uploads = workspace / APP_DATA_DIR, workspace / UPLOADS_DIR
    rows: list[dict[str, Any]] = []
    missing: list[str] = []

    def row(
        row_id: str,
        label: str,
        hint: str,
        measured: Measured | None,
        items: list[dict[str, Any]],
    ) -> int:
        if measured is None:
            missing.append(row_id)
            return 0
        rows.append(
            {
                "id": row_id,
                "label": label,
                "hint": hint,
                "bytes": measured.total,
                "items": items,
            }
        )
        return measured.total

    # The workspace is measured as its top-level entries in one du, so its folders can be listed
    # under its row (the largest first) without walking the tree twice; a listing that fails
    # measures the directory whole and lists nothing.
    try:
        entries = sorted(workspace.iterdir())
    except OSError:
        entries = []
    folders = {path for path in entries if path.is_dir()}
    workspace_measured = measure(entries or [workspace], [app_data, uploads])
    counted = row(
        "workspace",
        "Workspace files",
        "documents, project folders, the workspace's own code",
        workspace_measured,
        _items(
            Measured(
                workspace_measured.total,
                {
                    path: size
                    for path, size in workspace_measured.by_path.items()
                    if path in folders
                },
            )
            if workspace_measured is not None
            else None,
            WORKSPACE_FOLDER_HINTS,
            lambda path: path.name,
            WORKSPACE_ITEM_COUNT,
        ),
    )
    counted += row(
        "uploads", "Uploads", "files attached to chats", measure([uploads], []), []
    )
    # The App data row lists what it could measure; an app directory it could not, or a listing
    # that failed, leaves the row short and reports it missing like a row of its own would be.
    app_items: list[dict[str, Any]] = []
    try:
        app_dirs = (
            sorted(path for path in app_data.iterdir() if path.is_dir())
            if app_data.is_dir()
            else []
        )
    except OSError:
        app_dirs = []
        missing.append("apps")
    for app_dir in app_dirs:
        measured = measure([app_dir], [])
        if measured is None:
            if "apps" not in missing:
                missing.append("apps")
        else:
            app_items.append(
                {"name": app_dir.name, "hint": "", "bytes": measured.total}
            )
    app_items.sort(key=lambda item: item["bytes"], reverse=True)
    rows.append(
        {
            "id": "apps",
            "label": "App data",
            "hint": "what your apps store",
            "bytes": sum(item["bytes"] for item in app_items),
            "items": app_items,
        }
    )
    counted += rows[-1]["bytes"]
    caches = measure([home / cache for cache in CACHE_DIRS], [])
    counted += row(
        "caches",
        "Package caches",
        "safe to clear; downloaded again when needed",
        caches,
        _items(
            caches,
            CACHE_HINTS,
            lambda path: str(path.relative_to(home))
            if path.is_relative_to(home)
            else path.name,
            None,
        ),
    )
    # The rest of the disk: the machine's system software (the workspace's tools, measured
    # without the home tree, which the rows above hold) and whatever else is on the disk.
    other = max(0, disk_used - counted)
    system = measure([root], [home, *SYSTEM_EXCLUDES])
    other_items: list[dict[str, Any]] = []
    if system is not None and system.total:
        software = min(other, system.total)
        other_items.append(
            {
                "name": "System software",
                "hint": "the workspace's tools",
                "bytes": software,
            }
        )
        if other - software:
            other_items.append(
                {
                    "name": "Other files on the machine's disk",
                    "hint": "outside this workspace",
                    "bytes": other - software,
                }
            )
    rows.append(
        {
            "id": "other",
            "label": "Everything else",
            "hint": "the workspace's system software and the rest of the machine's disk",
            "bytes": other,
            "items": other_items,
        }
    )
    return rows, missing


class DiskScanner:
    """Runs at most one walk at a time on a thread, and keeps the last result (or failure) with its time."""

    def __init__(
        self,
        run: Callable[[], tuple[list[dict[str, Any]], list[str]]],
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._run = run
        self._clock = clock
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "state": "idle",
            "scanned_at": None,
            "rows": [],
            "missing": [],
        }

    def start(self) -> bool:
        """Start a walk unless one is running; whether this call started one."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._state = {**self._state, "state": "scanning"}
            self._thread = threading.Thread(
                target=self._walk, name="disk-scan", daemon=True
            )
            self._thread.start()
            return True

    def _walk(self) -> None:
        try:
            rows, missing = self._run()
        except OSError:
            # The disk could not be read at all (not one row's du, which scan reports as missing):
            # the walk ends failed rather than staying "scanning" with the button disabled.
            rows, missing, state = [], [], "failed"
        else:
            state = "done"
        with self._lock:
            self._state = {
                "state": state,
                "scanned_at": self._clock().isoformat(),
                "rows": rows,
                "missing": missing,
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def wait(self, timeout_seconds: float) -> None:
        """Block until the running walk ends (tests)."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout_seconds)

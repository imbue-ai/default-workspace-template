"""Reading and writing the files the editor opens, keeping the versions it saves over, and finding markdown files.

The editor opens files under the workspace user's home, as Files does, and never one in a credentials folder. It
writes only markdown and text files, refuses a save when the file changed since the version the edit started from,
and copies the version it replaces into its history folder first, so a mistake costs at most one save.
"""

import hashlib
import os
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from markdown_editor_app.data_types import FileRecord
from markdown_editor_app.data_types import FileVersion
from markdown_editor_app.data_types import FoundFile
from markdown_editor_app.errors import FileRefusedError

WRITABLE_SUFFIXES: Final[frozenset[str]] = frozenset({".md", ".markdown", ".mdown", ".mkd", ".txt"})
LISTED_SUFFIXES: Final[frozenset[str]] = frozenset({".md", ".markdown"})
READABLE_LIMIT_BYTES: Final[int] = 5 * 1024 * 1024
# A folder holding credentials is never opened, whatever its parent.
REFUSED_PARTS: Final[frozenset[str]] = frozenset({".secrets"})
# Folders too large and machine-made to be worth listing. Hidden folders are skipped too: they hold machine state,
# tickets, other repos and app data, and people's own files are in visible folders.
SKIPPED_DIRS: Final[frozenset[str]] = frozenset({"node_modules", "venv", "__pycache__", "dist", "build", "vendor"})
FOUND_LIMIT: Final[int] = 200
SEARCH_DEPTH: Final[int] = 7
SEARCH_BUDGET_SECONDS: Final[float] = 3.0
SEARCH_CACHE_SECONDS: Final[float] = 30.0
HISTORY_KEEP: Final[int] = 30
HISTORY_SOURCE_FILENAME: Final[str] = "source.txt"

HTTP_BAD_REQUEST: Final[int] = 400
HTTP_FORBIDDEN: Final[int] = 403
HTTP_NOT_FOUND: Final[int] = 404
HTTP_CONFLICT: Final[int] = 409
HTTP_CONTENT_TOO_LARGE: Final[int] = 413
HTTP_UNSUPPORTED_MEDIA_TYPE: Final[int] = 415


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class WorkspaceFiles:
    """The files under ``root`` the editor may open, and where it keeps the versions it saved over.

    ``root`` is kept under the name it was given (on Studio ``/home/user`` is a link), so paths are answered as
    people know them rather than wherever the link points. A relative path names a file under ``search_root``.
    """

    def __init__(self, root: Path, search_root: Path, history_dir: Path) -> None:
        self.root_name = root.absolute()
        self.root = root.resolve()
        self.search_root = search_root.resolve()
        self.history_dir = history_dir
        self._write_lock = threading.Lock()

    def resolve(self, path_text: str) -> Path:
        """The absolute file a request names, refused when it is outside the root or under a credentials folder."""
        if not path_text or "\x00" in path_text:
            raise FileRefusedError("No file named.", HTTP_BAD_REQUEST)
        raw = Path(path_text).expanduser()
        if not raw.is_absolute():
            raw = self.search_root / raw
        path = raw.resolve()
        if path != self.root and self.root not in path.parents:
            raise FileRefusedError(f"Only files under {self.root_name} can be opened.", HTTP_FORBIDDEN)
        if any(part in REFUSED_PARTS for part in path.parts):
            raise FileRefusedError("Files in a credentials folder are not opened here.", HTTP_FORBIDDEN)
        return path

    def shown(self, path: Path) -> str:
        """``path`` (resolved) as people know it: under the root's own name rather than wherever that link points."""
        if path == self.root:
            return str(self.root_name)
        if self.root in path.parents:
            return str(self.root_name / path.relative_to(self.root))
        return str(path)

    def _existing_file(self, path_text: str) -> Path:
        path = self.resolve(path_text)
        if not path.is_file():
            raise FileRefusedError(f"There is no file at {self.shown(path)}.", HTTP_NOT_FOUND)
        return path

    def read(self, path_text: str) -> FileRecord:
        """The file's text and version, and whether the editor may save it."""
        path = self._existing_file(path_text)
        stat = path.stat()
        if stat.st_size > READABLE_LIMIT_BYTES:
            raise FileRefusedError(
                f"{path.name} is {stat.st_size // 1024 // 1024} MB; the editor opens files up to 5 MB.",
                HTTP_CONTENT_TOO_LARGE,
            )
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise FileRefusedError(f"{path.name} is not a text file.", HTTP_UNSUPPORTED_MEDIA_TYPE) from error
        return FileRecord(
            path=self.shown(path),
            name=path.name,
            text=text,
            sha256=sha256_of(data),
            size=stat.st_size,
            mtime=stat.st_mtime,
            is_writable=path.suffix.lower() in WRITABLE_SUFFIXES and os.access(path, os.W_OK),
        )

    def raw_path(self, path_text: str) -> Path:
        """The file a raw read or download serves, exactly as saved."""
        return self._existing_file(path_text)

    def version(self, path_text: str) -> FileVersion:
        """The file's current version."""
        path = self._existing_file(path_text)
        return FileVersion(sha256=sha256_of(path.read_bytes()), mtime=path.stat().st_mtime)

    def write(self, path_text: str, text: str, base_sha256: str | None) -> FileVersion:
        """Write ``text`` to the file, refused when it changed since ``base_sha256``; None writes regardless.

        The version being replaced is kept in the history folder first. The new text goes to a temporary file
        beside the old one, which then replaces it, so a reader never sees half a save.
        """
        path = self.resolve(path_text)
        if path.suffix.lower() not in WRITABLE_SUFFIXES:
            raise FileRefusedError(
                f"Only markdown and text files are saved here, not {path.suffix or 'this kind of file'}.",
                HTTP_FORBIDDEN,
            )
        if not path.is_file():
            raise FileRefusedError(f"There is no file at {self.shown(path)}.", HTTP_NOT_FOUND)
        data = text.encode("utf-8")
        with self._write_lock:
            current = path.read_bytes()
            current_sha256 = sha256_of(current)
            if base_sha256 is not None and base_sha256 != current_sha256:
                raise FileRefusedError("The file was changed outside the editor.", HTTP_CONFLICT)
            if current == data:
                return FileVersion(sha256=current_sha256, mtime=path.stat().st_mtime)
            self._keep_history(path, current)
            mode = path.stat().st_mode & 0o7777
            handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".saving", dir=path.parent)
            temporary = Path(temporary_name)
            is_replaced = False
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(data)
                os.chmod(temporary, mode)
                os.replace(temporary, path)
                is_replaced = True
            finally:
                if not is_replaced:
                    temporary.unlink(missing_ok=True)
        return FileVersion(sha256=sha256_of(data), mtime=path.stat().st_mtime)

    def history_folder(self, path: Path) -> Path:
        """Where the versions saved over of ``path`` are kept: one folder per file, named for it."""
        digest = hashlib.sha256(str(path).encode()).hexdigest()[:16]
        return self.history_dir / f"{path.name}-{digest}"

    def _keep_history(self, path: Path, data: bytes) -> None:
        folder = self.history_folder(path)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / HISTORY_SOURCE_FILENAME).write_text(str(path) + "\n")
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"-{time.time_ns() % 1_000_000_000:09d}"
        (folder / f"{stamp}{path.suffix}").write_bytes(data)
        versions = sorted(item for item in folder.iterdir() if item.name != HISTORY_SOURCE_FILENAME)
        for old in versions[:-HISTORY_KEEP]:
            old.unlink(missing_ok=True)


def scan_markdown(
    files: WorkspaceFiles, root: Path, budget_seconds: float = SEARCH_BUDGET_SECONDS
) -> tuple[FoundFile, ...]:
    """Markdown files under ``root``, most recently changed first; the walk stops after ``budget_seconds``."""
    found: list[FoundFile] = []
    base_depth = len(root.parts)
    deadline = time.monotonic() + budget_seconds
    for folder, dirs, names in os.walk(root):
        if time.monotonic() > deadline:
            break
        depth = len(Path(folder).parts) - base_depth
        dirs[:] = sorted(name for name in dirs if name not in SKIPPED_DIRS and not name.startswith("."))
        if depth >= SEARCH_DEPTH:
            dirs[:] = []
        for name in names:
            if Path(name).suffix.lower() not in LISTED_SUFFIXES:
                continue
            full = Path(folder) / name
            try:
                stat = full.stat()
            except OSError:
                continue
            found.append(
                FoundFile(path=files.shown(full.resolve()), name=name, size=stat.st_size, mtime=stat.st_mtime)
            )
    found.sort(key=lambda item: item.mtime, reverse=True)
    return tuple(found[:FOUND_LIMIT])


class MarkdownFinder:
    """The markdown files the start page offers, cached.

    A fresh list is answered as is; a stale one is answered at once while a new scan runs in the background, since a
    cold scan of a big workspace takes seconds on a sandboxed disk.
    """

    def __init__(self, files: WorkspaceFiles, cache_seconds: float = SEARCH_CACHE_SECONDS) -> None:
        self.files = files
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._refreshing = threading.Event()
        self._cached_at: float | None = None
        self._cached: tuple[FoundFile, ...] = ()

    def _scan_and_keep(self) -> tuple[FoundFile, ...]:
        found = scan_markdown(self.files, self.files.search_root)
        with self._lock:
            self._cached_at = time.monotonic()
            self._cached = found
        return found

    def _refresh_in_background(self) -> None:
        try:
            self._scan_and_keep()
        finally:
            self._refreshing.clear()

    def find(self) -> Sequence[FoundFile]:
        with self._lock:
            cached_at = self._cached_at
            cached = self._cached
        if cached_at is None:
            return self._scan_and_keep()
        if time.monotonic() - cached_at >= self.cache_seconds and not self._refreshing.is_set():
            self._refreshing.set()
            threading.Thread(target=self._refresh_in_background, daemon=True).start()
        return cached

    @property
    def is_refreshing(self) -> bool:
        """Whether a background scan is running."""
        return self._refreshing.is_set()

    def warm(self) -> None:
        """Scan once in the background, so the first start page answers at once."""
        threading.Thread(target=self.find, daemon=True).start()

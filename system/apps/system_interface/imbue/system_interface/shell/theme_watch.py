"""The theme folders, watched (docs/system/blueprint/workspace-themes/, section 5.2): when a file a theme is drawn
from changes -- an agent editing a theme, an icon landing in data/.themes/, an app manifest declaring or renaming the
parts an overlay styles -- every window is sent the catalog again, so the pages wearing the theme load its new
revision and the picker lists what is there now."""

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer as _Observer
from watchdog.observers.api import BaseObserver
from workspace_themes.catalog import APPS_DIRECTORY
from workspace_themes.contract import BUILTIN_THEMES_DIRECTORY
from workspace_themes.contract import GENERATED_ICONS_DIRECTORY
from workspace_themes.contract import WORKSPACE_THEMES_DIRECTORY
from workspace_themes.data_types import ThemeCatalog
from workspace_themes.interfaces import ThemeCatalogLoaderInterface

from imbue.imbue_common.mutable_model import MutableModel

# A burst of writes (a theme saved file by file, an icon fitted then installed) is answered once, after it settles.
THEME_CHANGE_SETTLE_SECONDS: Final[float] = 0.4


class _AnyChangeHandler(FileSystemEventHandler):
    """Calls back on every event that changes a file or folder under the watched tree."""

    on_change: Callable[[], None]

    def _fire(self, event: FileSystemEvent) -> None:
        self.on_change()

    on_modified = _fire
    on_created = _fire
    on_deleted = _fire
    on_moved = _fire


def _catalog_signature(catalog: ThemeCatalog) -> tuple[tuple[str, str, bool], ...]:
    return tuple((str(entry.id), str(entry.revision), entry.is_available) for entry in catalog.entries)


class ThemeCatalogWatch(MutableModel):
    """Watches the theme roots, the generated icons, and the app manifests, and tells every window when the catalog
    changes."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    repo_root: Path = Field(frozen=True, description="The workspace's repo root")
    loader: ThemeCatalogLoaderInterface = Field(frozen=True, description="Reads the catalog")
    on_catalog_changed: Callable[[], None] = Field(frozen=True, description="Sends every window the catalog")

    _observer: BaseObserver | None = PrivateAttr(default=None)
    _watched: list[tuple[Path, bool]] = PrivateAttr(default_factory=list)
    _timer: threading.Timer | None = PrivateAttr(default=None)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # A timer already running is not stopped by a cancel, so two checks can overlap; this keeps them one at a time.
    _check_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _last_signature: tuple[tuple[str, str, bool], ...] | None = PrivateAttr(default=None)
    _is_stopped: bool = PrivateAttr(default=False)

    def _watched_directories(self) -> list[tuple[Path, bool]]:
        """Each directory to watch and whether recursively: a theme root that exists, else the folder it would be
        made in, so its creation is seen too; and the apps folder and each app's own folder, only one level deep
        (an app's manifest says which overlays are checked and against which parts, and an app's subfolders hold
        its builds)."""
        watched: list[tuple[Path, bool]] = []
        for relative in (BUILTIN_THEMES_DIRECTORY, WORKSPACE_THEMES_DIRECTORY, GENERATED_ICONS_DIRECTORY):
            directory = self.repo_root / relative
            if directory.is_dir():
                watched.append((directory, True))
            elif directory.parent.is_dir():
                watched.append((directory.parent, False))
            else:
                # No parent to watch either (a workspace with no data/ yet); a change under another root rebuilds
                # the watch, which picks this root up once it exists.
                continue
        apps_directory = self.repo_root.joinpath(*APPS_DIRECTORY)
        if apps_directory.is_dir():
            watched.append((apps_directory, False))
            watched.extend((child, False) for child in sorted(apps_directory.iterdir()) if child.is_dir())
        elif apps_directory.parent.is_dir():
            watched.append((apps_directory.parent, False))
        else:
            # No system/ either; a theme root's change rebuilds the watch, which picks the apps up once they exist.
            pass
        return list(dict.fromkeys(watched))

    def start(self) -> None:
        self._last_signature = _catalog_signature(self.loader.load())
        self._start_observer()

    def _start_observer(self) -> None:
        handler = _AnyChangeHandler()
        handler.on_change = self.schedule_check
        observer = _Observer()
        watched = self._watched_directories()
        for directory, is_recursive in watched:
            observer.schedule(handler, str(directory), recursive=is_recursive)
        observer.daemon = True
        try:
            observer.start()
        except OSError as error:
            logger.opt(exception=error).error("Failed to watch the theme folders")
            return
        self._observer = observer
        self._watched = watched

    def _stop_observer(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def stop(self) -> None:
        with self._lock:
            # An event the observer delivers before it stops must not start a check that reports after shutdown.
            self._is_stopped = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        # A check under way may be replacing the observer; stop the one it leaves.
        with self._check_lock:
            self._stop_observer()

    def schedule_check(self) -> None:
        """Check once a burst of changes settles; nothing once the watch has stopped."""
        with self._lock:
            if self._is_stopped:
                return
            if self._timer is not None:
                self._timer.cancel()
            timer = threading.Timer(THEME_CHANGE_SETTLE_SECONDS, self.check_now)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def check_now(self) -> None:
        """Read the catalog and, when a theme came, went, changed, or changed availability, tell every window;
        nothing once the watch has stopped."""
        with self._check_lock:
            with self._lock:
                if self._is_stopped:
                    return
            # A theme root made since the watch began (the first workspace theme's themes/) is watched from now on.
            if self._observer is not None and self._watched_directories() != self._watched:
                self._stop_observer()
                self._start_observer()
            signature = _catalog_signature(self.loader.load())
            with self._lock:
                if signature == self._last_signature:
                    return
                self._last_signature = signature
            logger.info("The theme catalog changed; telling every window")
            self.on_catalog_changed()

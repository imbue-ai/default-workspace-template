"""Desktops: the shared desktops of desktop-interface contracts.md section 4.1, stored in ``desktops.json``."""

import re
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Final

from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId
from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import AppPin
from imbue.system_interface.shell.data_types import ClientRecord
from imbue.system_interface.shell.data_types import Desktop
from imbue.system_interface.shell.data_types import DesktopChangeOutcome
from imbue.system_interface.shell.data_types import DesktopDeleteOutcome
from imbue.system_interface.shell.data_types import DesktopShortcut
from imbue.system_interface.shell.data_types import DesktopsChangeOutcome
from imbue.system_interface.shell.data_types import DesktopsDocument
from imbue.system_interface.shell.data_types import GridCell
from imbue.system_interface.shell.data_types import Wallpaper
from imbue.system_interface.shell.data_types import Window
from imbue.system_interface.shell.desktop_document import DESKTOPS_FILE_VERSION
from imbue.system_interface.shell.desktop_document import with_pinned_windows_ensured
from imbue.system_interface.shell.desktop_document import with_shortcut
from imbue.system_interface.shell.desktop_document import with_shortcut_moved
from imbue.system_interface.shell.desktop_document import with_window_location
from imbue.system_interface.shell.desktop_document import with_window_opened
from imbue.system_interface.shell.desktop_document import without_pin_marks
from imbue.system_interface.shell.desktop_document import without_shortcut
from imbue.system_interface.shell.desktop_document import without_window
from imbue.system_interface.shell.errors import DesktopConflictError
from imbue.system_interface.shell.errors import DesktopNotFoundError
from imbue.system_interface.shell.errors import DesktopValueError
from imbue.system_interface.shell.errors import LastDesktopError
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.primitives import GLYPH_COUNT
from imbue.system_interface.shell.primitives import SharingMode
from imbue.system_interface.shell.primitives import WindowId
from imbue.system_interface.shell.primitives import WindowPath
from imbue.system_interface.shell.primitives import WindowTitle
from imbue.system_interface.shell.state_files import STATE_FILES_LOCK
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

DESKTOPS_FILENAME: Final[str] = "desktops.json"

# The one desktop a fresh workspace starts with (desktop plan section 3.2).
DEFAULT_DESKTOP_NAME: Final[str] = "Home"
DEFAULT_DESKTOP_COLOR: Final[str] = "#2f6b4f"
DEFAULT_DESKTOP_GLYPH: Final[int] = 0

_COLOR_PATTERN: Final[re.Pattern[str]] = re.compile(r"#[0-9a-fA-F]{6}")
_SLUG_STRIP_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")


@pure
def slugify_desktop_name(name: str) -> DesktopId:
    """The id a desktop name shortens to; raises DesktopValueError when nothing usable remains."""
    slug = _SLUG_STRIP_PATTERN.sub("-", name.strip().lower()).strip("-")
    if not slug:
        raise DesktopValueError(f"Desktop name {name!r} contains no usable characters")
    try:
        return DesktopId(slug)
    except ValueError as e:
        raise DesktopValueError(f"Desktop name {name!r} cannot be a desktop id: {e}") from e


@pure
def validated_desktop_name(name: str) -> str:
    trimmed = name.strip()
    if not trimmed:
        raise DesktopValueError("Desktop name is empty")
    return trimmed


@pure
def validated_desktop_color(color: str) -> str:
    trimmed = color.strip()
    if _COLOR_PATTERN.fullmatch(trimmed) is None:
        raise DesktopValueError(f"Desktop color {color!r} is not a '#RRGGBB' hex string")
    return trimmed


@pure
def validated_desktop_glyph(glyph: int) -> int:
    if not 0 <= glyph < GLYPH_COUNT:
        raise DesktopValueError(f"Desktop glyph {glyph} is outside the range 0..{GLYPH_COUNT - 1}")
    return glyph


@pure
def default_desktop(shortcuts: Sequence[DesktopShortcut]) -> Desktop:
    return Desktop(
        id=slugify_desktop_name(DEFAULT_DESKTOP_NAME),
        name=DEFAULT_DESKTOP_NAME,
        color=DEFAULT_DESKTOP_COLOR,
        glyph=DEFAULT_DESKTOP_GLYPH,
        sharing=SharingMode.SHARED,
        wallpaper=None,
        shortcuts=tuple(shortcuts),
        windows=(),
    )


@pure
def resolve_active_desktop(record: ClientRecord | None, desktops: Sequence[Desktop]) -> DesktopId | None:
    """The desktop a client is on (desktop contracts.md section 4.3): its stored active desktop when a desktop of
    that id exists, else the first desktop; None with no desktops."""
    if not desktops:
        return None
    if record is not None and record.active_desktop is not None:
        if any(desktop.id == record.active_desktop for desktop in desktops):
            return record.active_desktop
    return desktops[0].id


@pure
def find_desktop_by_name_or_id(desktops: Sequence[Desktop], requested: str) -> Desktop | None:
    wanted = requested.strip().lower()
    for desktop in desktops:
        if desktop.id == requested or desktop.name.strip().lower() == wanted:
            return desktop
    return None


class DesktopStore(MutableModel):
    """Reads and writes ``desktops.json`` under the shell's state lock."""

    state_directory: Path = Field(frozen=True, description="The shell's state directory")

    def _path(self) -> Path:
        return self.state_directory / DESKTOPS_FILENAME

    def _read_unlocked(self) -> DesktopsDocument | None:
        """The stored document, or None when the file is absent, unreadable, or of another version (logged)."""
        raw = read_json_object(self._path())
        if raw is None:
            return None
        if raw.get("version") != DESKTOPS_FILE_VERSION:
            logger.warning("Ignored a desktops file of version {!r} at {}", raw.get("version"), self._path())
            return None
        try:
            return DesktopsDocument.model_validate(raw)
        except ValidationError as e:
            logger.warning("Ignored an unreadable desktops file at {}: {}", self._path(), e.errors()[0]["msg"])
            return None

    def _write_unlocked(self, document: DesktopsDocument) -> None:
        write_json_atomic(self._path(), document.model_dump(mode="json"))

    def list_desktops(self) -> list[Desktop]:
        """Every desktop in creation order; an absent or unreadable file lists none (``ensure_default`` seeds one)."""
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
        return list(document.desktops) if document is not None else []

    def ensure_default(self, seed_shortcuts: Callable[[], Sequence[DesktopShortcut]]) -> list[Desktop]:
        """The desktops, after creating the default one when the file holds none (desktop plan section 3.2)."""
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            if document is not None and document.desktops:
                return list(document.desktops)
            seeded = DesktopsDocument(version=DESKTOPS_FILE_VERSION, desktops=(default_desktop(seed_shortcuts()),))
            self._write_unlocked(seeded)
            logger.info(
                "Created the default desktop {!r} with {} shortcut(s)",
                DEFAULT_DESKTOP_NAME,
                len(seeded.desktops[0].shortcuts),
            )
            return list(seeded.desktops)

    def ensure_pinned_windows(self, pins: Sequence[AppPin], now: datetime) -> DesktopsChangeOutcome:
        """The desktops, after every desktop holds exactly one pinned window per pinned app and no pin mark of an app
        that is no longer pinned (pinned-taskbar-entries plan section 3.2); written only when something changed."""
        pinned_app_names = {app_pin.app for app_pin in pins}
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            desktops = document.desktops if document is not None else ()
            ensured = tuple(
                with_pinned_windows_ensured(without_pin_marks(desktop, pinned_app_names), pins, now).desktop
                for desktop in desktops
            )
            if all(after is before for after, before in zip(ensured, desktops, strict=True)):
                return DesktopsChangeOutcome(desktops=desktops, is_written=False)
            self._write_unlocked(DesktopsDocument(version=DESKTOPS_FILE_VERSION, desktops=ensured))
        logger.info("Reconciled the pinned windows of {} desktop(s) for {} pinned app(s)", len(ensured), len(pins))
        return DesktopsChangeOutcome(desktops=ensured, is_written=True)

    def create_desktop(
        self, name: str, color: str, glyph: int, shortcuts: Sequence[DesktopShortcut], windows: Sequence[Window]
    ) -> Desktop:
        """Register a new desktop with its seeded shortcuts and pinned windows and no wallpaper; two names that
        shorten to one id conflict."""
        desktop = Desktop(
            id=slugify_desktop_name(name),
            name=validated_desktop_name(name),
            color=validated_desktop_color(color),
            glyph=validated_desktop_glyph(glyph),
            sharing=SharingMode.SHARED,
            wallpaper=None,
            shortcuts=tuple(shortcuts),
            windows=tuple(windows),
        )
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            existing_desktops = document.desktops if document is not None else ()
            existing = next((candidate for candidate in existing_desktops if candidate.id == desktop.id), None)
            if existing is not None:
                raise DesktopConflictError(
                    f"Desktop name {name!r} conflicts with existing desktop {existing.name!r} (both shorten to '{desktop.id}')"
                )
            self._write_unlocked(
                DesktopsDocument(version=DESKTOPS_FILE_VERSION, desktops=(*existing_desktops, desktop))
            )
        return desktop

    def update_settings(self, desktop_id: str, name: str, color: str, glyph: int, sharing: SharingMode) -> Desktop:
        """Replace one desktop's display metadata and sharing mode; the id, wallpaper, shortcuts, and windows stay."""
        return self._replace(
            desktop_id,
            lambda desktop: desktop.model_copy_update(
                to_update(desktop.field_ref().name, validated_desktop_name(name)),
                to_update(desktop.field_ref().color, validated_desktop_color(color)),
                to_update(desktop.field_ref().glyph, validated_desktop_glyph(glyph)),
                to_update(desktop.field_ref().sharing, sharing),
            ),
        )

    def set_wallpaper(self, desktop_id: str, wallpaper: Wallpaper | None) -> Desktop:
        return self._replace(
            desktop_id, lambda desktop: desktop.model_copy_update(to_update(desktop.field_ref().wallpaper, wallpaper))
        )

    def delete_desktop(self, desktop_id: str) -> DesktopDeleteOutcome:
        """Delete a desktop (its windows with it) and name the desktop its clients fall back to; the last one is refused."""
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            desktops = document.desktops if document is not None else ()
            doomed = next((desktop for desktop in desktops if desktop.id == desktop_id), None)
            if doomed is None:
                raise DesktopNotFoundError(desktop_id)
            remaining = tuple(desktop for desktop in desktops if desktop.id != desktop_id)
            if not remaining:
                raise LastDesktopError(f"Desktop {doomed.name!r} is the last one and cannot be deleted")
            self._write_unlocked(DesktopsDocument(version=DESKTOPS_FILE_VERSION, desktops=remaining))
        return DesktopDeleteOutcome(deleted=doomed, fallback_desktop_id=remaining[0].id)

    def set_shortcut(self, desktop_id: str, shortcut: DesktopShortcut) -> Desktop:
        return self._replace(desktop_id, lambda desktop: with_shortcut(desktop, shortcut))

    def move_shortcut(self, desktop_id: str, app: AppName, launch: LaunchPathId, cell: GridCell) -> Desktop:
        return self._replace(desktop_id, lambda desktop: with_shortcut_moved(desktop, app, launch, cell))

    def remove_shortcut(self, desktop_id: str, app: AppName, launch: LaunchPathId) -> Desktop:
        return self._replace(desktop_id, lambda desktop: without_shortcut(desktop, app, launch))

    def open_window(self, desktop_id: str, window: Window) -> Desktop:
        return self._replace(desktop_id, lambda desktop: with_window_opened(desktop, window))

    def close_window(self, desktop_id: str, window_id: WindowId) -> DesktopChangeOutcome:
        """Drop the window from the desktop; a window the desktop does not hold changes nothing (idempotent)."""
        return self._replace_if_changed(desktop_id, lambda desktop: without_window(desktop, window_id))

    def set_window_location(
        self, desktop_id: str, window_id: WindowId, path: WindowPath, title: WindowTitle
    ) -> DesktopChangeOutcome:
        """Store what a page reported for its window; raises WindowNotFoundError; a report that changes nothing writes nothing."""
        return self._replace_if_changed(
            desktop_id, lambda desktop: with_window_location(desktop, window_id, path, title)
        )

    def _replace_if_changed(self, desktop_id: str, transform: Callable[[Desktop], Desktop]) -> DesktopChangeOutcome:
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            desktops = document.desktops if document is not None else ()
            current = next((desktop for desktop in desktops if desktop.id == desktop_id), None)
            if current is None:
                raise DesktopNotFoundError(desktop_id)
            edited = transform(current)
            if edited == current:
                return DesktopChangeOutcome(desktop=current, is_written=False)
            self._write_unlocked(
                DesktopsDocument(
                    version=DESKTOPS_FILE_VERSION,
                    desktops=tuple(edited if desktop.id == desktop_id else desktop for desktop in desktops),
                )
            )
        return DesktopChangeOutcome(desktop=edited, is_written=True)

    def _replace(self, desktop_id: str, transform: Callable[[Desktop], Desktop]) -> Desktop:
        return self._replace_if_changed(desktop_id, transform).desktop

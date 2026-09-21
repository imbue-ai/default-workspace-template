"""Desktops: the shared desktops of desktop-interface contracts.md section 4.1, stored in ``desktops.json``."""

import re
from collections.abc import Callable
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Any
from typing import Final

from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId
from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import ClientRecord
from imbue.system_interface.shell.data_types import Desktop
from imbue.system_interface.shell.data_types import DesktopChangeOutcome
from imbue.system_interface.shell.data_types import DesktopDeleteOutcome
from imbue.system_interface.shell.data_types import DesktopShortcut
from imbue.system_interface.shell.data_types import DesktopsDocument
from imbue.system_interface.shell.data_types import GridCell
from imbue.system_interface.shell.data_types import Wallpaper
from imbue.system_interface.shell.data_types import Window
from imbue.system_interface.shell.desktop_document import DESKTOPS_FILE_VERSION
from imbue.system_interface.shell.desktop_document import with_shortcut
from imbue.system_interface.shell.desktop_document import with_shortcut_moved
from imbue.system_interface.shell.desktop_document import with_window_location
from imbue.system_interface.shell.desktop_document import with_window_opened
from imbue.system_interface.shell.desktop_document import without_shortcut
from imbue.system_interface.shell.desktop_document import without_window
from imbue.system_interface.shell.errors import DesktopConflictError
from imbue.system_interface.shell.errors import DesktopNotFoundError
from imbue.system_interface.shell.errors import DesktopValueError
from imbue.system_interface.shell.errors import LastDesktopError
from imbue.system_interface.shell.identity import RequestIdentity
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.primitives import GLYPH_COUNT
from imbue.system_interface.shell.primitives import UserId
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

# Each glyph's signature colour, in glyph order: the frontend's ``SQUIGGLE_GLYPHS`` palette, which is what the
# settings dialog offers, so a desktop the shell names itself wears a colour the dialog could have picked.
DESKTOP_GLYPH_COLORS: Final[tuple[str, ...]] = (
    "#F0603A",
    "#16A34A",
    "#E3A400",
    "#45BC4E",
    "#12B5A5",
    "#17A2C4",
    "#3B82F6",
    "#7C5CFF",
    "#B455E8",
    "#EC4899",
)
# What a user's desktop is called when their identity offers no usable name.
FALLBACK_USER_DESKTOP_NAME: Final[str] = "Guest"
# The key desktops.json carried per desktop before every desktop was shared (the sharing mode); an old file may
# still hold it.
# CLEANUP: drop ``_RETIRED_DESKTOP_KEYS`` and the strip in ``_read_unlocked`` around late November 2026, once every
# workspace has rewritten its desktops.json without the key (the first write after this release does).
_RETIRED_DESKTOP_KEYS: Final[frozenset[str]] = frozenset({"sharing"})

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
def next_glyph_index(used_glyphs: Sequence[int]) -> int:
    """The glyph a desktop the shell names gets: the first nobody uses, then repeating (the frontend's rule)."""
    used = set(used_glyphs)
    for index in range(GLYPH_COUNT):
        if index not in used:
            return index
    return len(used_glyphs) % GLYPH_COUNT


@pure
def unique_desktop_name(base: str, existing: Sequence[Desktop]) -> str:
    """``base``, or ``base 2``, ``base 3``, ... until neither the name nor its id is taken (the frontend's rule)."""
    taken_names = {desktop.name.strip().lower() for desktop in existing}
    taken_ids = {str(desktop.id) for desktop in existing}
    candidate = base
    suffix = 1
    while candidate.lower() in taken_names or str(slugify_desktop_name(candidate)) in taken_ids:
        suffix += 1
        candidate = f"{base} {suffix}"
    return candidate


@pure
def _sluggable(name: str) -> bool:
    try:
        slugify_desktop_name(name)
    except DesktopValueError:
        return False
    return True


@pure
def desktop_name_for_user(identity: RequestIdentity, existing: Sequence[Desktop]) -> str:
    """What a visiting user's desktop is called: their display name, else the local part of their email, else a
    fallback, made unique among the existing desktops."""
    candidates = [
        (identity.display_name or "").strip(),
        (identity.email or "").split("@")[0].strip(),
        FALLBACK_USER_DESKTOP_NAME,
    ]
    base = next(candidate for candidate in candidates if candidate and _sluggable(candidate))
    return unique_desktop_name(base, existing)


@pure
def default_desktop(shortcuts: Sequence[DesktopShortcut]) -> Desktop:
    return Desktop(
        id=slugify_desktop_name(DEFAULT_DESKTOP_NAME),
        name=DEFAULT_DESKTOP_NAME,
        color=DEFAULT_DESKTOP_COLOR,
        glyph=DEFAULT_DESKTOP_GLYPH,
        wallpaper=None,
        shortcuts=tuple(shortcuts),
        windows=(),
    )


@pure
def _without_retired_keys(raw: dict[str, Any]) -> dict[str, Any]:
    """The raw desktops document with the keys this version no longer stores dropped from every desktop."""
    desktops = raw.get("desktops")
    if not isinstance(desktops, list):
        return raw
    return {
        **raw,
        "desktops": [
            {key: value for key, value in desktop.items() if key not in _RETIRED_DESKTOP_KEYS}
            if isinstance(desktop, dict)
            else desktop
            for desktop in desktops
        ],
    }


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
def desktop_kept_by_returning_client(
    record: ClientRecord | None, user_id: UserId, desktop_ids: AbstractSet[DesktopId]
) -> DesktopId | None:
    """The desktop a visiting user's client keeps on arrival (desktop plan section 3.10): the one it was on, when it
    last arrived as this same user and that desktop still exists; None for a new client, for one that last arrived
    as someone else (or anonymously), and for one whose desktop is gone."""
    if record is None or record.user_id != user_id or record.active_desktop is None:
        return None
    return record.active_desktop if record.active_desktop in desktop_ids else None


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
            return DesktopsDocument.model_validate(_without_retired_keys(raw))
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

    def create_desktop(self, name: str, color: str, glyph: int, shortcuts: Sequence[DesktopShortcut]) -> Desktop:
        """Register a new desktop with no windows and no wallpaper; two names that shorten to one id conflict."""
        return self.add_desktop(
            Desktop(
                id=slugify_desktop_name(name),
                name=validated_desktop_name(name),
                color=validated_desktop_color(color),
                glyph=validated_desktop_glyph(glyph),
                wallpaper=None,
                shortcuts=tuple(shortcuts),
                windows=(),
            )
        )

    def add_desktop(self, desktop: Desktop) -> Desktop:
        """Append a fully formed desktop (a created or a seeded one); an id already taken conflicts."""
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            existing_desktops = document.desktops if document is not None else ()
            existing = next((candidate for candidate in existing_desktops if candidate.id == desktop.id), None)
            if existing is not None:
                raise DesktopConflictError(
                    f"Desktop name {desktop.name!r} conflicts with existing desktop {existing.name!r} "
                    f"(both shorten to '{desktop.id}')"
                )
            self._write_unlocked(
                DesktopsDocument(version=DESKTOPS_FILE_VERSION, desktops=(*existing_desktops, desktop))
            )
        return desktop

    def update_settings(self, desktop_id: str, name: str, color: str, glyph: int) -> Desktop:
        """Replace one desktop's display metadata; the id, wallpaper, shortcuts, and windows stay."""
        return self._replace(
            desktop_id,
            lambda desktop: desktop.model_copy_update(
                to_update(desktop.field_ref().name, validated_desktop_name(name)),
                to_update(desktop.field_ref().color, validated_desktop_color(color)),
                to_update(desktop.field_ref().glyph, validated_desktop_glyph(glyph)),
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

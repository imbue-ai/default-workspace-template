"""Desktops: the shared desktops of desktop-interface contracts.md section 4.1, stored in ``desktops.json``, and
the default shortcuts offered to them (section 4.1a), in ``default_shortcuts_offered.json``."""

import re
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Final

from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId
from app_manifest.registry import RegistryRow
from loguru import logger
from pydantic import Field
from workspace_layout.identity import RequestIdentity
from workspace_layout.primitives import DesktopId
from workspace_layout.primitives import ParticipantKey
from workspace_layout.primitives import UserId
from workspace_layout.primitives import WindowId
from workspace_layout.primitives import WindowPath
from workspace_layout.primitives import WindowTitle
from workspace_layout.records import ClientRecord
from workspace_layout.records import Desktop
from workspace_layout.records import DesktopShortcut
from workspace_layout.records import GridCell
from workspace_layout.records import ShortcutTarget
from workspace_layout.records import Wallpaper
from workspace_layout.records import Window
from workspace_themes.primitives import ThemeId

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import AppPin
from imbue.system_interface.shell.data_types import AppsOfferedDocument
from imbue.system_interface.shell.data_types import DefaultShortcutsOfferedDocument
from imbue.system_interface.shell.data_types import DesktopChangeOutcome
from imbue.system_interface.shell.data_types import DesktopDeleteOutcome
from imbue.system_interface.shell.data_types import DesktopThemesDocument
from imbue.system_interface.shell.data_types import DesktopsChangeOutcome
from imbue.system_interface.shell.data_types import DesktopsDocument
from imbue.system_interface.shell.data_types import ThemeChoices
from imbue.system_interface.shell.desktop_document import DESKTOPS_FILE_VERSION
from imbue.system_interface.shell.desktop_document import default_shortcut_targets
from imbue.system_interface.shell.desktop_document import first_default_shortcut_targets
from imbue.system_interface.shell.desktop_document import shortcut_targets_on
from imbue.system_interface.shell.desktop_document import with_default_shortcuts_offered
from imbue.system_interface.shell.desktop_document import with_pinned_windows_ensured
from imbue.system_interface.shell.desktop_document import with_shortcut
from imbue.system_interface.shell.desktop_document import with_shortcut_moved
from imbue.system_interface.shell.desktop_document import with_window_location
from imbue.system_interface.shell.desktop_document import with_window_opened
from imbue.system_interface.shell.desktop_document import without_pin_marks
from imbue.system_interface.shell.desktop_document import without_shortcut
from imbue.system_interface.shell.desktop_document import without_shortcuts_of_unregistered_apps
from imbue.system_interface.shell.desktop_document import without_window
from imbue.system_interface.shell.errors import DesktopConflictError
from imbue.system_interface.shell.errors import DesktopNotFoundError
from imbue.system_interface.shell.errors import DesktopValueError
from imbue.system_interface.shell.errors import LastDesktopError
from imbue.system_interface.shell.ownership import reordered_desktops
from imbue.system_interface.shell.ownership import with_visitor_owners
from imbue.system_interface.shell.primitives import GLYPH_COUNT
from imbue.system_interface.shell.state_files import STATE_FILES_LOCK
from imbue.system_interface.shell.state_files import parse_versioned_document
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

DESKTOPS_FILENAME: Final[str] = "desktops.json"
# The workspace's default theme and each desktop's own (workspace-themes plan section 5.2).
DESKTOP_THEMES_FILENAME: Final[str] = "desktop_themes.json"
DESKTOP_THEMES_FILE_VERSION: Final[int] = 1
# The default shortcuts the shell has offered (desktop plan section 3.2).
DEFAULT_SHORTCUTS_OFFERED_FILENAME: Final[str] = "default_shortcuts_offered.json"
DEFAULT_SHORTCUTS_OFFERED_FILE_VERSION: Final[int] = 2
# The record's version while it named apps (``AppsOfferedDocument``).
_APPS_OFFERED_FILE_VERSION: Final[int] = 1

# The one desktop a fresh workspace starts with (desktop plan section 3.2).
DEFAULT_DESKTOP_NAME: Final[str] = "Home"
DEFAULT_DESKTOP_COLOR: Final[str] = "#2f6b4f"
DEFAULT_DESKTOP_GLYPH: Final[int] = 0

# Each glyph's signature colour, in glyph order. The shell still fills both fields for a desktop it names itself,
# since the record keeps them (the multiplayer spec, S7).
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
# CLEANUP: drop ``_RETIRED_WINDOW_KEYS`` and its strip in ``_without_retired_keys`` around late December 2026, once
# every workspace has rewritten its desktops.json without the key (the first write after this release does).
_RETIRED_WINDOW_KEYS: Final[frozenset[str]] = frozenset({"is_settling"})

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
def desktop_name_for_user(display_name: str | None, email: str | None, existing: Sequence[Desktop]) -> str:
    """What a visiting user's desktop is called: their profile's display name, else the local part of their email,
    else a fallback, made unique among the existing desktops."""
    candidates = [
        (display_name or "").strip(),
        (email or "").split("@")[0].strip(),
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
def _without_retired_window_keys(desktop: dict[str, Any]) -> dict[str, Any]:
    windows = desktop.get("windows")
    if not isinstance(windows, list):
        return desktop
    return {
        **desktop,
        "windows": [
            {key: value for key, value in window.items() if key not in _RETIRED_WINDOW_KEYS}
            if isinstance(window, dict)
            else window
            for window in windows
        ],
    }


@pure
def _without_retired_keys(raw: dict[str, Any]) -> dict[str, Any]:
    """The raw desktops document with the keys this version no longer stores dropped from every desktop and window."""
    desktops = raw.get("desktops")
    if not isinstance(desktops, list):
        return raw
    return {
        **raw,
        "desktops": [
            _without_retired_window_keys(
                {key: value for key, value in desktop.items() if key not in _RETIRED_DESKTOP_KEYS}
            )
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


def _target_sort_key(target: ShortcutTarget) -> tuple[str, str]:
    return (str(target.app), str(target.launch))


@pure
def _offered_without_a_record(desktops: Sequence[Desktop], rows: Sequence[RegistryRow]) -> frozenset[ShortcutTarget]:
    """What counts as offered when no record can be read: the first default shortcut of every app with a shortcut on
    ``desktops``, as a record of apps would have said."""
    return first_default_shortcut_targets(rows, frozenset(target.app for target in shortcut_targets_on(desktops)))


class DesktopStore(MutableModel):
    """Reads and writes ``desktops.json``, ``desktop_themes.json`` and ``default_shortcuts_offered.json`` under the
    shell's state lock."""

    state_directory: Path = Field(frozen=True, description="The shell's state directory")

    def _path(self) -> Path:
        return self.state_directory / DESKTOPS_FILENAME

    def _read_unlocked(self) -> DesktopsDocument | None:
        """The stored document, or None when the file is absent, unreadable, or of another version (logged)."""
        raw = read_json_object(self._path())
        return parse_versioned_document(
            _without_retired_keys(raw) if raw is not None else None,
            DesktopsDocument,
            DESKTOPS_FILE_VERSION,
            self._path(),
        )

    def _write_unlocked(self, document: DesktopsDocument) -> None:
        write_json_atomic(self._path(), document.model_dump(mode="json"))

    def _themes_path(self) -> Path:
        return self.state_directory / DESKTOP_THEMES_FILENAME

    def _read_themes_unlocked(self) -> ThemeChoices:
        """The recorded choices; none made when the file is absent, unreadable, or of another version (logged)."""
        document = parse_versioned_document(
            read_json_object(self._themes_path()),
            DesktopThemesDocument,
            DESKTOP_THEMES_FILE_VERSION,
            self._themes_path(),
        )
        if document is None:
            return ThemeChoices(default=None, theme_by_desktop_id={})
        return ThemeChoices(default=document.default, theme_by_desktop_id=dict(document.desktops))

    def _write_themes_unlocked(self, choices: ThemeChoices) -> None:
        document = DesktopThemesDocument(
            version=DESKTOP_THEMES_FILE_VERSION, default=choices.default, desktops=dict(choices.theme_by_desktop_id)
        )
        write_json_atomic(self._themes_path(), document.model_dump(mode="json"))

    def _forget_desktop_themes_unlocked(self, desktop_ids: AbstractSet[DesktopId]) -> None:
        """Drop the theme choices of ``desktop_ids``, so an id that comes back never wears a theme nobody chose for it;
        written only when one of them had a choice."""
        choices = self._read_themes_unlocked()
        kept = {key: value for key, value in choices.theme_by_desktop_id.items() if key not in desktop_ids}
        if len(kept) != len(choices.theme_by_desktop_id):
            self._write_themes_unlocked(ThemeChoices(default=choices.default, theme_by_desktop_id=kept))

    def _offered_path(self) -> Path:
        return self.state_directory / DEFAULT_SHORTCUTS_OFFERED_FILENAME

    def _read_offered_unlocked(self, rows: Sequence[RegistryRow]) -> frozenset[ShortcutTarget] | None:
        """The targets recorded as offered, or None when the file is absent, unreadable, or of another version
        (logged). A record of apps, written while an app had one default shortcut, offered each app's first one; it is
        rewritten as those targets once there are rows to read them from, and reads as None until then."""
        raw = read_json_object(self._offered_path())
        if raw is not None and raw.get("version") == _APPS_OFFERED_FILE_VERSION:
            legacy = parse_versioned_document(
                raw, AppsOfferedDocument, _APPS_OFFERED_FILE_VERSION, self._offered_path()
            )
            if legacy is None or not rows:
                return None
            targets = first_default_shortcut_targets(rows, frozenset(legacy.apps))
            self._write_offered_unlocked(targets)
            return targets
        document = parse_versioned_document(
            raw, DefaultShortcutsOfferedDocument, DEFAULT_SHORTCUTS_OFFERED_FILE_VERSION, self._offered_path()
        )
        return frozenset(document.targets) if document is not None else None

    def _write_offered_unlocked(self, targets: AbstractSet[ShortcutTarget]) -> None:
        ordered: list[ShortcutTarget] = sorted(targets, key=_target_sort_key)
        document = DefaultShortcutsOfferedDocument(
            version=DEFAULT_SHORTCUTS_OFFERED_FILE_VERSION, targets=tuple(ordered)
        )
        write_json_atomic(self._offered_path(), document.model_dump(mode="json"))

    def _record_offered_unlocked(
        self, targets: AbstractSet[ShortcutTarget], desktops_before: Sequence[Desktop], rows: Sequence[RegistryRow]
    ) -> None:
        """Add ``targets``, whose default shortcuts the desktops write that follows places, to the offered record; with
        no record yet, or no desktop before that write, the record starts over from what the desktops before it count
        as offered. With no record, desktops before, and no rows to read their apps' defaults from, it stays unwritten
        for the next reconcile with rows. It is written first, so a failure between the two never leaves the record of
        replaced desktops beside new ones."""
        recorded = self._read_offered_unlocked(rows)
        if recorded is None and desktops_before and not rows:
            return
        kept = (
            recorded if recorded is not None and desktops_before else _offered_without_a_record(desktops_before, rows)
        )
        offered = kept | targets
        if offered != recorded:
            self._write_offered_unlocked(offered)

    def list_desktops(self) -> list[Desktop]:
        """Every desktop in display order (creation order, as ``reorder_desktops`` rearranged it); an absent or
        unreadable file lists none (``ensure_default`` seeds one)."""
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
        return list(document.desktops) if document is not None else []

    def ensure_default(self, seed_shortcuts: Callable[[], Sequence[DesktopShortcut]]) -> list[Desktop]:
        """The desktops, after creating the default one when the file holds none (desktop plan section 3.2), with the
        offered record started over from the targets of its seeded shortcuts and no desktop keeping a theme of its
        own."""
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            if document is not None and document.desktops:
                return list(document.desktops)
            seeded = DesktopsDocument(version=DESKTOPS_FILE_VERSION, desktops=(default_desktop(seed_shortcuts()),))
            self._write_offered_unlocked(shortcut_targets_on(seeded.desktops))
            self._forget_desktop_themes_unlocked(frozenset(self._read_themes_unlocked().theme_by_desktop_id))
            self._write_unlocked(seeded)
            logger.info(
                "Created the default desktop {!r} with {} shortcut(s)",
                DEFAULT_DESKTOP_NAME,
                len(seeded.desktops[0].shortcuts),
            )
            return list(seeded.desktops)

    def reconcile_shortcuts_with(self, rows: Sequence[RegistryRow]) -> DesktopsChangeOutcome:
        """The desktops, after every desktop drops the shortcuts of apps no row registers
        (``without_shortcuts_of_unregistered_apps``) and holds each default shortcut not yet offered
        (``with_default_shortcuts_offered``), with the record holding what is offered. Each dropped shortcut's target
        leaves the record, so it is offered again if its app registers again; a default shortcut the user had already
        taken off every desktop stays offered, so that removal sticks. desktops.json is written only when a desktop
        changed. A registry with no rows is one nothing has registered in yet, never one every app left, so it drops
        nothing and leaves the record as it is."""
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            desktops = document.desktops if document is not None else ()
            if not desktops:
                return DesktopsChangeOutcome(desktops=desktops, is_written=False)
            recorded = self._read_offered_unlocked(rows)
            offered = recorded if recorded is not None else _offered_without_a_record(desktops, rows)
            kept = (
                tuple(without_shortcuts_of_unregistered_apps(desktop, rows) for desktop in desktops)
                if rows
                else desktops
            )
            dropped = shortcut_targets_on(desktops) - shortcut_targets_on(kept)
            offered = offered - dropped
            reconciled = tuple(with_default_shortcuts_offered(desktop, rows, offered) for desktop in kept)
            changed_count = sum(1 for after, before in zip(reconciled, desktops, strict=True) if after is not before)
            if changed_count:
                self._write_unlocked(DesktopsDocument(version=DESKTOPS_FILE_VERSION, desktops=reconciled))
            newly_offered = default_shortcut_targets(rows) - offered
            if rows and offered | newly_offered != recorded:
                self._write_offered_unlocked(offered | newly_offered)
        if dropped:
            logger.info(
                "Removed the desktop shortcuts of {}, which no app registry row names",
                sorted({target.app for target in dropped}),
            )
        if newly_offered:
            logger.info(
                "Offered the default shortcuts {} ({} desktop(s) changed)",
                sorted(f"{target.app}/{target.launch}" for target in newly_offered),
                changed_count,
            )
        return DesktopsChangeOutcome(desktops=reconciled, is_written=changed_count > 0)

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

    def adopt_visitor_desktops(self, owner_by_desktop_id: Mapping[DesktopId, ParticipantKey]) -> DesktopsChangeOutcome:
        """The desktops after each one with no owner that ``owner_by_desktop_id`` names is given that owner; written
        only when one was."""
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            desktops = document.desktops if document is not None else ()
            adopted = with_visitor_owners(desktops, owner_by_desktop_id)
            if adopted == desktops:
                return DesktopsChangeOutcome(desktops=desktops, is_written=False)
            self._write_unlocked(DesktopsDocument(version=DESKTOPS_FILE_VERSION, desktops=adopted))
        return DesktopsChangeOutcome(desktops=adopted, is_written=True)

    def create_desktop(
        self,
        name: str,
        color: str,
        glyph: int,
        shortcuts: Sequence[DesktopShortcut],
        windows: Sequence[Window],
        # Whose desktop it is: None for the workspace owner's (the multiplayer spec, section 8.1).
        owner: ParticipantKey | None,
        rows: Sequence[RegistryRow],
    ) -> Desktop:
        """Register a new desktop with its seeded shortcuts (whose targets are recorded as offered) and pinned windows
        and no wallpaper; two names that shorten to one id conflict."""
        desktop = Desktop(
            id=slugify_desktop_name(name),
            name=validated_desktop_name(name),
            color=validated_desktop_color(color),
            glyph=validated_desktop_glyph(glyph),
            wallpaper=None,
            shortcuts=tuple(shortcuts),
            windows=tuple(windows),
            owner=owner,
        )
        return self.add_desktop(desktop, shortcut_targets_on((desktop,)), rows)

    def add_desktop(
        self, desktop: Desktop, offered_targets: AbstractSet[ShortcutTarget], rows: Sequence[RegistryRow]
    ) -> Desktop:
        """Append a fully formed desktop (a created or a seeded one), recording ``offered_targets`` (the default
        shortcut targets it holds) as offered and dropping any theme choice its id left behind; an id already taken
        conflicts."""
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            existing_desktops = document.desktops if document is not None else ()
            existing = next((candidate for candidate in existing_desktops if candidate.id == desktop.id), None)
            if existing is not None:
                raise DesktopConflictError(
                    f"Desktop name {desktop.name!r} conflicts with existing desktop {existing.name!r} "
                    f"(both shorten to '{desktop.id}')"
                )
            self._record_offered_unlocked(offered_targets, existing_desktops, rows)
            self._forget_desktop_themes_unlocked({desktop.id})
            self._write_unlocked(
                DesktopsDocument(version=DESKTOPS_FILE_VERSION, desktops=(*existing_desktops, desktop))
            )
        return desktop

    def update_settings(self, desktop_id: str, name: str, color: str | None, glyph: int | None) -> Desktop:
        """Rename one desktop, and replace its colour and glyph when given (the desktops phase shows neither; a
        caller from before it still sends them); the id, owner, wallpaper, shortcuts, and windows stay."""
        return self._replace(
            desktop_id,
            lambda desktop: desktop.model_copy_update(
                to_update(desktop.field_ref().name, validated_desktop_name(name)),
                to_update(
                    desktop.field_ref().color, validated_desktop_color(color) if color is not None else desktop.color
                ),
                to_update(
                    desktop.field_ref().glyph, validated_desktop_glyph(glyph) if glyph is not None else desktop.glyph
                ),
            ),
        )

    def reorder_desktops(self, ordered_own_ids: Sequence[DesktopId], identity: RequestIdentity) -> list[Desktop]:
        """Put the requester's own desktops in the order given, the others keeping their places (the multiplayer
        spec, section 8.2); raises DesktopValueError for an order that is not exactly their own desktops."""
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            desktops = document.desktops if document is not None else ()
            reordered = reordered_desktops(desktops, ordered_own_ids, identity)
            if reordered != desktops:
                self._write_unlocked(DesktopsDocument(version=DESKTOPS_FILE_VERSION, desktops=reordered))
        return list(reordered)

    def set_wallpaper(self, desktop_id: str, wallpaper: Wallpaper | None) -> Desktop:
        return self._replace(
            desktop_id, lambda desktop: desktop.model_copy_update(to_update(desktop.field_ref().wallpaper, wallpaper))
        )

    def read_theme_choices(self) -> ThemeChoices:
        """The workspace's default theme and each desktop's own; a desktop missing from them wears the default."""
        with STATE_FILES_LOCK:
            return self._read_themes_unlocked()

    def set_default_theme(self, theme: ThemeId | None) -> ThemeChoices:
        """Record the workspace's default theme; none wears the standard one."""
        with STATE_FILES_LOCK:
            choices = self._read_themes_unlocked()
            updated = ThemeChoices(default=theme, theme_by_desktop_id=choices.theme_by_desktop_id)
            self._write_themes_unlocked(updated)
        return updated

    def set_theme(self, desktop_id: str, theme: ThemeId | None) -> Desktop:
        """Record the theme a desktop wears, or (none) that it wears the default; raises DesktopNotFoundError for a
        desktop there is not."""
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            desktop = next(
                (
                    candidate
                    for candidate in (document.desktops if document is not None else ())
                    if candidate.id == desktop_id
                ),
                None,
            )
            if desktop is None:
                raise DesktopNotFoundError(desktop_id)
            choices = self._read_themes_unlocked()
            theme_by_desktop_id = {
                key: value for key, value in choices.theme_by_desktop_id.items() if key != desktop.id
            }
            if theme is not None:
                theme_by_desktop_id[desktop.id] = theme
            self._write_themes_unlocked(ThemeChoices(default=choices.default, theme_by_desktop_id=theme_by_desktop_id))
        return desktop

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
            self._forget_desktop_themes_unlocked({doomed.id})
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

import re
import secrets
from enum import auto
from typing import Any
from typing import Final
from typing import Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema
from pydantic_core import core_schema

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.system_interface.shell.errors import InvalidShellValueError

# A desktop id is the slugified desktop name (desktop contracts.md section 1).
_DESKTOP_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
# A client id is the uuid the browser keeps in local storage (desktop contracts.md section 1), and it
# names a layout file, so it is held to a filename-safe alphabet.
_CLIENT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAVE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^save-[0-9a-f]{16}$")
_WINDOW_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^win-[0-9a-f]{16}$")
# A wallpaper reference's ``name`` is a file name without its extension (desktop contracts.md section 4.4).
_WALLPAPER_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MINTED_ID_BYTES: Final[int] = 8

# A window's path (desktop contracts.md section 1).
MAX_WINDOW_PATH_LENGTH: Final[int] = 2048
MAX_WINDOW_TITLE_LENGTH: Final[int] = 256

# A desktop's ``glyph`` indexes the frontend's squiggle table, which has exactly ten entries.
GLYPH_COUNT: Final[int] = 10


def _string_schema(cls: type, handler: GetCoreSchemaHandler) -> CoreSchema:
    return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())


class ClientId(NonEmptyStr):
    """One connected browser context, as its stored id names it."""

    def __new__(cls, value: str) -> Self:
        if not _CLIENT_ID_PATTERN.fullmatch(value):
            raise InvalidShellValueError(f"invalid client id {value!r}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(cls, handler)


class SaveId(NonEmptyStr):
    """A layout save's id: ``save-<16 hex>``, minted by the window that saved."""

    def __new__(cls, value: str) -> Self:
        if not _SAVE_ID_PATTERN.fullmatch(value):
            raise InvalidShellValueError(f"invalid save id {value!r}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(cls, handler)


def mint_save_id() -> SaveId:
    """A save id for a write the shell makes itself (a window mints its own)."""
    return SaveId(f"save-{secrets.token_hex(_MINTED_ID_BYTES)}")


class ClientActivityKind(LowerCaseStrEnum):
    """What a client-activity report records: a message a client sent to an app's page (a wire value)."""

    MESSAGE = auto()


class AppLifecycleAction(LowerCaseStrEnum):
    """The two verbs the workspace has for an app's supervised program (a wire value: the route's last path segment)."""

    STOP = auto()
    START = auto()


class DesktopId(NonEmptyStr):
    """The slugified name of a desktop, stable across renames (desktop contracts.md section 1)."""

    def __new__(cls, value: str) -> Self:
        if not _DESKTOP_ID_PATTERN.fullmatch(value):
            raise InvalidShellValueError(f"invalid desktop id {value!r}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(cls, handler)


class WindowId(NonEmptyStr):
    """A window id the shell minted: ``win-<16 hex>``, never reused."""

    def __new__(cls, value: str) -> Self:
        if not _WINDOW_ID_PATTERN.fullmatch(value):
            raise InvalidShellValueError(f"invalid window id {value!r}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(cls, handler)


class WindowPath(str):
    """A path under an app's origin: one leading slash, at most 2048 characters, no control characters, query allowed."""

    def __new__(cls, value: str) -> Self:
        if not value.startswith("/") or value.startswith("//"):
            raise InvalidShellValueError(f"invalid window path {value!r}: a path starts with a single '/'")
        if len(value) > MAX_WINDOW_PATH_LENGTH:
            raise InvalidShellValueError(f"invalid window path: at most {MAX_WINDOW_PATH_LENGTH} characters")
        if any(character < " " or character == "\x7f" for character in value):
            raise InvalidShellValueError(f"invalid window path {value!r}: control characters are not allowed")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(cls, handler)


class WindowTitle(str):
    """What a page last reported as its title, trimmed, at most 256 characters; empty means the app's display name."""

    def __new__(cls, value: str) -> Self:
        trimmed = value.strip()
        if len(trimmed) > MAX_WINDOW_TITLE_LENGTH:
            raise InvalidShellValueError(f"invalid window title: at most {MAX_WINDOW_TITLE_LENGTH} characters")
        return super().__new__(cls, trimmed)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(cls, handler)


class WallpaperName(NonEmptyStr):
    """A wallpaper's file name without its extension."""

    def __new__(cls, value: str) -> Self:
        if not _WALLPAPER_NAME_PATTERN.fullmatch(value):
            raise InvalidShellValueError(f"invalid wallpaper name {value!r}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(cls, handler)


def mint_window_id() -> WindowId:
    return WindowId(f"win-{secrets.token_hex(_MINTED_ID_BYTES)}")


class SharingMode(LowerCaseStrEnum):
    """Whether a desktop is shared with everyone on the workspace or personal (a wire value; V1 enforces nothing)."""

    SHARED = auto()
    PERSONAL = auto()


class WindowState(UpperCaseStrEnum):
    """How one client shows a window: at its frame, snapped to a half, or maximized (a wire value)."""

    NORMAL = auto()
    SNAPPED_LEFT = auto()
    SNAPPED_RIGHT = auto()
    MAXIMIZED = auto()


class WallpaperKind(LowerCaseStrEnum):
    """Where a wallpaper image comes from: the shell's bundled assets or the workspace's wallpapers directory."""

    BUNDLED = auto()
    FILE = auto()


class IfPresent(LowerCaseStrEnum):
    """What an open does about a window of the app already at the path: focus it, or open another (a wire value)."""

    FOCUS = auto()
    NEW = auto()


class ShortcutTargetKind(LowerCaseStrEnum):
    """What a desktop shortcut runs; V1 has one kind, a launch path (a wire value)."""

    LAUNCH = auto()

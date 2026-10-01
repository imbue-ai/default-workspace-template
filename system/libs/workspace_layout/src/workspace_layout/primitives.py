import re
from enum import auto
from typing import Any
from typing import Final
from typing import Self

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.primitives import NonEmptyStr
from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema
from pydantic_core import core_schema

from workspace_layout.errors import InvalidLayoutValueError

# A desktop id is the slugified desktop name (desktop contracts.md section 1).
_DESKTOP_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
# A value that names a file on disk (a client id its layout files, a user id the user's presence file, a wallpaper
# name its image file) is held to one filename-safe alphabet.
FILENAME_SAFE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOW_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^win-[0-9a-f]{16}$")
# A window's path and title (desktop contracts.md section 1).
MAX_WINDOW_PATH_LENGTH: Final[int] = 2048
MAX_WINDOW_TITLE_LENGTH: Final[int] = 256


def _string_schema(cls: type) -> CoreSchema:
    return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())


class ClientId(NonEmptyStr):
    """One connected browser context, as its stored id names it: the uuid the browser keeps in local storage
    (desktop contracts.md section 1)."""

    def __new__(cls, value: str) -> Self:
        if not FILENAME_SAFE_PATTERN.fullmatch(value):
            raise InvalidLayoutValueError(f"invalid client id {value!r}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(cls)


class DesktopId(NonEmptyStr):
    """The slugified name of a desktop, stable across renames (desktop contracts.md section 1)."""

    def __new__(cls, value: str) -> Self:
        if not _DESKTOP_ID_PATTERN.fullmatch(value):
            raise InvalidLayoutValueError(f"invalid desktop id {value!r}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(cls)


class WindowId(NonEmptyStr):
    """A window id the shell minted: ``win-<16 hex>``, never reused."""

    def __new__(cls, value: str) -> Self:
        if not _WINDOW_ID_PATTERN.fullmatch(value):
            raise InvalidLayoutValueError(f"invalid window id {value!r}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(cls)


class UserId(NonEmptyStr):
    """A signed-in account's user id as the identity header carries it; it names the user's state files, so it is held
    to a filename-safe alphabet."""

    def __new__(cls, value: str) -> Self:
        if not FILENAME_SAFE_PATTERN.fullmatch(value):
            raise InvalidLayoutValueError(f"invalid user id {value!r}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(cls)


class WindowPath(str):
    """A path under an app's origin: one leading slash, at most 2048 characters, no control characters, query allowed."""

    def __new__(cls, value: str) -> Self:
        if not value.startswith("/") or value.startswith("//"):
            raise InvalidLayoutValueError(f"invalid window path {value!r}: a path starts with a single '/'")
        if len(value) > MAX_WINDOW_PATH_LENGTH:
            raise InvalidLayoutValueError(f"invalid window path: at most {MAX_WINDOW_PATH_LENGTH} characters")
        if any(character < " " or character == "\x7f" for character in value):
            raise InvalidLayoutValueError(f"invalid window path {value!r}: control characters are not allowed")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(cls)


class WindowPage(WindowPath):
    """A page under an app's origin: a window path with no query string or fragment."""

    def __new__(cls, value: str) -> Self:
        if "?" in value or "#" in value:
            raise InvalidLayoutValueError(f"invalid page {value!r}: a page has no query string or fragment")
        return super().__new__(cls, value)


class WindowTitle(str):
    """What a page last reported as its title, trimmed, at most 256 characters; empty means the app's display name."""

    def __new__(cls, value: str) -> Self:
        trimmed = value.strip()
        if len(trimmed) > MAX_WINDOW_TITLE_LENGTH:
            raise InvalidLayoutValueError(f"invalid window title: at most {MAX_WINDOW_TITLE_LENGTH} characters")
        return super().__new__(cls, trimmed)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(cls)


class WallpaperName(NonEmptyStr):
    """A wallpaper's file name without its extension (desktop contracts.md section 4.6)."""

    def __new__(cls, value: str) -> Self:
        if not FILENAME_SAFE_PATTERN.fullmatch(value):
            raise InvalidLayoutValueError(f"invalid wallpaper name {value!r}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(cls)


class WallpaperKind(LowerCaseStrEnum):
    """Where a wallpaper image comes from: the shell's bundled assets or the workspace's wallpapers directory."""

    BUNDLED = auto()
    FILE = auto()


class ClientActivityKind(LowerCaseStrEnum):
    """What a client-activity report records: a message a client sent to an app's page (a wire value)."""

    MESSAGE = auto()


class IfPresent(LowerCaseStrEnum):
    """What an open does about a window of the app already at the path: focus it, or open another (a wire value)."""

    FOCUS = auto()
    NEW = auto()


class WindowState(UpperCaseStrEnum):
    """How one client shows a window: at its frame, snapped to a half, or maximized (a wire value)."""

    NORMAL = auto()
    SNAPPED_LEFT = auto()
    SNAPPED_RIGHT = auto()
    MAXIMIZED = auto()


class ShowOutcome(LowerCaseStrEnum):
    """How a ``show`` op put the path on screen (a wire value): a window already showing it raised, a window on the
    same page navigated, the app's pinned window navigated, or a new window opened."""

    RAISED = auto()
    NAVIGATED = auto()
    PINNED = auto()
    OPENED = auto()


class ShortcutTargetKind(LowerCaseStrEnum):
    """What a desktop shortcut runs; V1 has one kind, a launch path (a wire value)."""

    LAUNCH = auto()


class SpecialWindow(LowerCaseStrEnum):
    """The window arguments that are not ids or app names: the requester's own window, which the op's ``requester``
    names, and the requester's app's pinned window on the target desktop (pinned-taskbar-entries plan section 4.8)."""

    SELF = auto()
    PINNED = auto()


class LayoutOp(LowerCaseStrEnum):
    """An op of the op route (desktop contracts.md section 8), as the body's ``op`` spells it."""

    CONTEXT = auto()
    # Read-only: answered with the inventory document (desktop contracts.md section 5.5).
    DESKTOPS = auto()
    LIST = auto()
    LOAD = auto()
    OPEN = auto()
    # Put a path of an app on the target client's screen, choosing the window itself.
    SHOW = auto()
    FOCUS = auto()
    MINIMIZE = auto()
    RESTORE = auto()
    MAXIMIZE = auto()
    PLACE = auto()
    CLOSE = auto()
    NAVIGATE = auto()
    SHORTCUTS = auto()
    SHORTCUT_SET = auto()
    SHORTCUT_MOVE = auto()
    SHORTCUT_REMOVE = auto()
    WALLPAPER = auto()
    # Change what is on screen without changing the files: they reach the browser as a ``layout_op`` message, as
    # does a ``show`` that lands on a pulled-out window.
    REFRESH = auto()
    RELOAD_SYSTEM_INTERFACE = auto()

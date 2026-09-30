import re
import secrets
from enum import auto
from typing import Any
from typing import Final
from typing import Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema
from pydantic_core import core_schema
from workspace_layout.primitives import FILENAME_SAFE_PATTERN
from workspace_layout.primitives import WindowId

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.system_interface.shell.errors import InvalidShellValueError

_SAVE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^save-[0-9a-f]{16}$")
_MINTED_ID_BYTES: Final[int] = 8

# A window's path (desktop contracts.md section 1).
MAX_WINDOW_PATH_LENGTH: Final[int] = 2048
MAX_WINDOW_TITLE_LENGTH: Final[int] = 256

# A desktop's ``glyph`` indexes the frontend's squiggle table.
GLYPH_COUNT: Final[int] = 10


def _string_schema(cls: type, handler: GetCoreSchemaHandler) -> CoreSchema:
    return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())


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


class AppLifecycleAction(LowerCaseStrEnum):
    """The two verbs the workspace has for an app's supervised program (a wire value: the route's last path segment)."""

    STOP = auto()
    START = auto()


class UserId(NonEmptyStr):
    """A signed-in account's user id as the identity header carries it; it names the user's state files, so it is held
    to a filename-safe alphabet."""

    def __new__(cls, value: str) -> Self:
        if not FILENAME_SAFE_PATTERN.fullmatch(value):
            raise InvalidShellValueError(f"invalid user id {value!r}")
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


class WindowPage(WindowPath):
    """A page under an app's origin: a window path with no query string or fragment."""

    def __new__(cls, value: str) -> Self:
        if "?" in value or "#" in value:
            raise InvalidShellValueError(f"invalid page {value!r}: a page has no query string or fragment")
        return super().__new__(cls, value)


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


def mint_window_id() -> WindowId:
    return WindowId(f"win-{secrets.token_hex(_MINTED_ID_BYTES)}")


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


class LaunchTargetKind(LowerCaseStrEnum):
    """Where a launch's page goes (post-launch-paths plan section 3.3): a new window, a window already at that path
    (else a new one), or a named window this client points at it (a wire value)."""

    NEW = auto()
    FOCUS = auto()
    WINDOW = auto()

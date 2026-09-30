import re
from enum import auto
from typing import Any
from typing import Final
from typing import Self

from imbue.imbue_common.enums import LowerCaseStrEnum
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

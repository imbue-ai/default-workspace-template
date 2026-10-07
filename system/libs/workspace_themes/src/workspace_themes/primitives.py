import re
from enum import auto
from pathlib import PurePosixPath
from typing import Any, Final, Self

from imbue.imbue_common.enums import LowerCaseStrEnum
from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

from workspace_themes.errors import InvalidThemeValueError

THEME_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
MAX_THEME_NAME_LENGTH: Final[int] = 48
MAX_THEME_DESCRIPTION_LENGTH: Final[int] = 200
MAX_THEME_AUTHOR_LENGTH: Final[int] = 80
HEX_COLOR_PATTERN: Final[re.Pattern[str]] = re.compile(r"^#[0-9a-fA-F]{6}$")


def _string_schema(cls: type) -> CoreSchema:
    return core_schema.no_info_after_validator_function(
        cls, core_schema.str_schema(), serialization=core_schema.to_string_ser_schema()
    )


class ThemeId(str):
    """A theme's id: its folder's name, lowercase letters, digits, and dashes."""

    def __new__(cls, value: str) -> Self:
        if THEME_ID_PATTERN.fullmatch(value) is None:
            raise InvalidThemeValueError(
                f"theme id {value!r} must be lowercase letters, digits, and dashes, starting with a letter or "
                "digit, at most 48 characters"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return _string_schema(cls)


class _BoundedText(str):
    """Trimmed text between one character and a maximum length."""

    max_length: int = 0
    label: str = ""

    def __new__(cls, value: str) -> Self:
        stripped = value.strip()
        if not stripped:
            raise InvalidThemeValueError(f"{cls.label} must not be empty")
        if len(stripped) > cls.max_length:
            raise InvalidThemeValueError(
                f"{cls.label} must be at most {cls.max_length} characters, got {len(stripped)}"
            )
        return super().__new__(cls, stripped)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return _string_schema(cls)


class ThemeName(_BoundedText):
    """What the theme picker shows."""

    max_length = MAX_THEME_NAME_LENGTH
    label = "name"


class ThemeDescription(_BoundedText):
    """One sentence about the theme."""

    max_length = MAX_THEME_DESCRIPTION_LENGTH
    label = "description"


class ThemeAuthor(_BoundedText):
    """Who made the theme."""

    max_length = MAX_THEME_AUTHOR_LENGTH
    label = "author"


class HexColor(str):
    """A color as ``#rrggbb``, kept in lowercase."""

    def __new__(cls, value: str) -> Self:
        if HEX_COLOR_PATTERN.fullmatch(value) is None:
            raise InvalidThemeValueError(f"color {value!r} must be written #rrggbb")
        return super().__new__(cls, value.lower())

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return _string_schema(cls)

    def as_rgb(self) -> tuple[int, int, int]:
        return (int(self[1:3], 16), int(self[3:5], 16), int(self[5:7], 16))


class ThemeRelativePath(str):
    """A path inside a theme folder: relative, written with ``/``, never leaving the folder."""

    def __new__(cls, value: str) -> Self:
        if not value or value.startswith("/") or "\\" in value:
            raise InvalidThemeValueError(
                f"path {value!r} must be relative to the theme folder and use /"
            )
        parts = PurePosixPath(value).parts
        if any(part in ("..", ".") for part in parts) or not parts:
            raise InvalidThemeValueError(
                f"path {value!r} must stay inside the theme folder"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return _string_schema(cls)


class ThemeRevision(str):
    """A digest that changes whenever a file a theme is drawn from changes."""

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return _string_schema(cls)


class ThemeSource(LowerCaseStrEnum):
    """Where a theme's folder is."""

    # system/themes/: shipped with the template.
    BUILTIN = auto()
    # themes/: made in this workspace.
    WORKSPACE = auto()


class TitleAlign(LowerCaseStrEnum):
    """Where the title sits in the title bar."""

    START = auto()
    CENTER = auto()


class ChromeSlot(LowerCaseStrEnum):
    """A place in the title bar: the app's icon, the title, or a control."""

    ICON = auto()
    TITLE = auto()
    REFRESH = auto()
    MENU = auto()
    MINIMIZE = auto()
    MAXIMIZE = auto()
    CLOSE = auto()


class IconFormat(LowerCaseStrEnum):
    """The file format of a theme's icons."""

    SVG = auto()
    PNG = auto()


class IconRendering(LowerCaseStrEnum):
    """How the shell scales a theme's icons."""

    SMOOTH = auto()
    PIXELATED = auto()


class IconBackground(LowerCaseStrEnum):
    """Whether an icon's corners are clear."""

    TRANSPARENT = auto()
    OPAQUE = auto()


class IconDerivation(LowerCaseStrEnum):
    """How the shell makes an icon from an app's standard one when the theme has none for the app."""

    # Draw the theme's generic program icon instead.
    NONE = auto()
    # Rasterize to the theme's canvas and scale it up without smoothing.
    PIXELATE = auto()
    # Rasterize, then map every pixel to the theme's palette or color count.
    QUANTIZE = auto()
    # Rasterize, then turn every opaque pixel to one of two colors.
    MONOCHROME = auto()

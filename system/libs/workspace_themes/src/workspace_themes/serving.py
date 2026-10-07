from pathlib import Path, PurePath, PurePosixPath
from typing import Final
from urllib.parse import quote

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from pydantic import Field

from workspace_themes.contract import (
    BUNDLE_FILE_NAME,
    FALLBACK_ICON_STEM,
    SERVED_FILE_SUFFIXES,
)
from workspace_themes.data_types import ThemeCatalog, ThemeEntry
from workspace_themes.errors import InvalidThemeValueError, ThemeFileNotFoundError
from workspace_themes.primitives import ThemeId, ThemeRelativePath

# The route every app that wears themes serves from its own origin (docs/system/blueprint/workspace-themes/, 5.1).
THEME_STATIC_ROUTE_PREFIX: Final[str] = "/_static/themes/"
CSS_MIMETYPE: Final[str] = "text/css"
# Every theme file is sent as the type its suffix names, never sniffed into another; an svg, which can carry script,
# is also sent as a document that runs nothing and loads nothing but its own inline styles.
_NOSNIFF_HEADERS: Final[dict[str, str]] = {"X-Content-Type-Options": "nosniff"}
_SVG_SUFFIX: Final[str] = ".svg"
_SVG_CONTENT_SECURITY_POLICY: Final[str] = (
    "default-src 'none'; style-src 'unsafe-inline'; sandbox"
)


class ThemeBundle(FrozenModel):
    """A theme's generated bundle."""

    css: str = Field(description="The bundle's text")


class ThemeFile(FrozenModel):
    """A file in a theme folder."""

    path: Path = Field(description="The file to send")


# What answers a request for a theme file.
ThemeAsset = ThemeBundle | ThemeFile


@pure
def build_bundle_css(entry: ThemeEntry) -> str:
    """The theme's bundle: an @import of every style file of its chain, base first, in the order they apply.

    Each import is relative to ``/_static/themes/<id>/theme.css``, so a base theme's file is reached through its own
    folder, and carries the theme's revision so an edit anywhere in the chain is fetched afresh.
    """
    lines = [f"/* {entry.id} ({' > '.join(entry.chain)}), revision {entry.revision} */"]
    for style_file in entry.style_files:
        url = f"../{quote(style_file.theme_id)}/{quote(style_file.path)}?v={entry.revision}"
        lines.append(f'@import url("{url}");')
    return "\n".join(lines) + "\n"


def _theme_file_path(entry: ThemeEntry, relative: str) -> Path:
    """A served file in the theme's own folder; raises ThemeFileNotFoundError for anything else."""
    try:
        relative_path = ThemeRelativePath(relative)
    except InvalidThemeValueError:
        raise ThemeFileNotFoundError(
            f"{relative!r} is not a path inside a theme folder"
        ) from None
    if PurePosixPath(relative_path).suffix.lower() not in SERVED_FILE_SUFFIXES:
        raise ThemeFileNotFoundError(
            f"{relative!r} is not a kind of file a theme serves"
        )
    folder = entry.folder.resolve()
    path = (folder / relative_path).resolve()
    if not path.is_relative_to(folder) or not path.is_file():
        raise ThemeFileNotFoundError(
            f"theme {str(entry.id)!r} has no file {relative!r}"
        )
    return path


def resolve_theme_asset(catalog: ThemeCatalog, request_path: str) -> ThemeAsset:
    """The answer to ``GET /_static/themes/<request_path>``; raises ThemeFileNotFoundError when there is none."""
    theme_part, separator, relative = request_path.partition("/")
    if not separator or not relative:
        raise ThemeFileNotFoundError(f"{request_path!r} names no file of a theme")
    try:
        theme_id = ThemeId(theme_part)
    except InvalidThemeValueError:
        raise ThemeFileNotFoundError(f"{theme_part!r} is not a theme id") from None
    entry = catalog.find_available(theme_id)
    if entry is None:
        raise ThemeFileNotFoundError(f"there is no available theme {str(theme_id)!r}")
    if relative == BUNDLE_FILE_NAME:
        return ThemeBundle(css=build_bundle_css(entry))
    return ThemeFile(path=_theme_file_path(entry, relative))


@pure
def theme_file_security_headers(path: PurePath) -> dict[str, str]:
    """The headers every answer with a theme's file carries (a style file, an asset, an icon), by its name."""
    if path.suffix.lower() == _SVG_SUFFIX:
        return {
            **_NOSNIFF_HEADERS,
            "Content-Security-Policy": _SVG_CONTENT_SECURITY_POLICY,
        }
    return dict(_NOSNIFF_HEADERS)


def _contained_icon(path: Path, theme_id: str, file_name: str) -> Path:
    """The icon file, when it is still inside the icons folder it was found in once links are followed."""
    if not path.is_file():
        raise ThemeFileNotFoundError(f"theme {theme_id!r} has no icon {file_name!r}")
    if not path.resolve().is_relative_to(path.parent.resolve()):
        raise ThemeFileNotFoundError(
            f"theme {theme_id!r}'s icon {file_name!r} is outside its icons folder"
        )
    return path


def resolve_theme_icon(catalog: ThemeCatalog, theme_id: str, file_name: str) -> Path:
    """An icon of an available theme, by ``<app>.<format>`` (curated before generated) or the generic ``app``."""
    entry = catalog.find_available(theme_id)
    if entry is None or entry.icons is None:
        raise ThemeFileNotFoundError(
            f"there is no available theme {theme_id!r} with icons"
        )
    icons = entry.icons
    stem, _, suffix = file_name.rpartition(".")
    if suffix != str(icons.spec.format):
        raise ThemeFileNotFoundError(
            f"theme {theme_id!r} draws {icons.spec.format} icons, not {file_name!r}"
        )
    if stem == FALLBACK_ICON_STEM and icons.fallback is not None:
        return _contained_icon(icons.fallback, theme_id, file_name)
    candidates = [*icons.curated_by_app.items(), *icons.generated_by_app.items()]
    path = next(
        (icon_path for app_name, icon_path in candidates if app_name == stem), None
    )
    if path is None:
        raise ThemeFileNotFoundError(f"theme {theme_id!r} has no icon {file_name!r}")
    return _contained_icon(path, theme_id, file_name)

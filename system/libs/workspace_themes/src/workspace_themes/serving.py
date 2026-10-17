import posixpath
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePath, PurePosixPath
from typing import Final
from urllib.parse import quote

import tinycss2
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field
from tinycss2 import ast
from tinycss2.serializer import serialize_string_value, serialize_url

from workspace_themes.contract import (
    BUNDLE_FILE_NAME,
    FALLBACK_ICON_STEM,
    SERVED_FILE_SUFFIXES,
    STYLE_FILE_SUFFIX,
    URL_FUNCTION_NAMES,
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


class ThemeStyleFile(FrozenModel):
    """A theme's style file, sent with the relative URLs in its custom properties made absolute."""

    path: Path = Field(description="The style file read")
    css: str = Field(description="The text to send")


# What answers a request for a theme file.
ThemeAsset = ThemeBundle | ThemeFile | ThemeStyleFile


@pure
def _absolute_url(reference: str, theme_id: str, file_directory: str) -> str | None:
    """A URL relative to a style file in the theme's folder, as a path from the origin; None for any other URL."""
    stripped = reference.strip()
    if (
        not stripped
        or stripped.startswith(("/", "#"))
        or ":" in stripped.split("/", 1)[0]
    ):
        return None
    joined = posixpath.normpath(posixpath.join(file_directory, stripped))
    if joined == ".." or joined.startswith("../"):
        return None
    return f"{THEME_STATIC_ROUTE_PREFIX}{quote(theme_id)}/{joined}"


def _absolutize_urls(
    tokens: Iterable[ast.Node], theme_id: str, file_directory: str
) -> bool:
    """Rewrite each relative URL among the tokens in place; whether any was."""
    is_changed = False
    for token in tokens:
        if isinstance(token, ast.URLToken):
            absolute = _absolute_url(token.value, theme_id, file_directory)
            if absolute is not None:
                token.value = absolute
                token.representation = f"url({serialize_url(absolute)})"
                is_changed = True
        elif isinstance(token, ast.FunctionBlock):
            if token.lower_name in URL_FUNCTION_NAMES:
                for argument in token.arguments:
                    if not isinstance(argument, ast.StringToken):
                        continue
                    absolute = _absolute_url(argument.value, theme_id, file_directory)
                    if absolute is not None:
                        argument.value = absolute
                        argument.representation = (
                            f'"{serialize_string_value(absolute)}"'
                        )
                        is_changed = True
            is_changed = (
                _absolutize_urls(token.arguments, theme_id, file_directory)
                or is_changed
            )
        elif isinstance(
            token,
            (ast.ParenthesesBlock, ast.SquareBracketsBlock, ast.CurlyBracketsBlock),
        ):
            is_changed = (
                _absolutize_urls(token.content, theme_id, file_directory) or is_changed
            )
        else:
            continue
    return is_changed


def _absolutize_custom_property_urls(
    rules: Sequence[ast.Node], theme_id: str, file_directory: str
) -> bool:
    is_changed = False
    for rule in rules:
        if isinstance(rule, ast.QualifiedRule):
            content = rule.content
        elif isinstance(rule, ast.AtRule) and rule.content is not None:
            nested = tinycss2.parse_rule_list(rule.content)
            is_changed = (
                _absolutize_custom_property_urls(nested, theme_id, file_directory)
                or is_changed
            )
            continue
        else:
            continue
        for declaration in tinycss2.parse_declaration_list(content):
            if isinstance(declaration, ast.Declaration) and declaration.name.startswith(
                "--"
            ):
                is_changed = (
                    _absolutize_urls(declaration.value, theme_id, file_directory)
                    or is_changed
                )
    return is_changed


def style_text_to_serve(css_text: str, theme_id: str, file_directory: str) -> str:
    """A theme's style file as it is sent: a relative URL in a custom property's value made absolute.

    A URL in a custom property is resolved where var() puts it, which may be a page's own style (the desktop's
    wallpaper is), so a URL written relative to the style file would be read against the page instead. Every other
    URL is left as written, since the browser resolves it against the style file.
    """
    rules = tinycss2.parse_stylesheet(css_text)
    if not _absolutize_custom_property_urls(rules, theme_id, file_directory):
        return css_text
    return tinycss2.serialize(rules)


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


def _is_existing_file(path: Path) -> bool:
    """Whether the path names a file; one the system cannot look up (a name too long) names none."""
    try:
        return path.is_file()
    except OSError as error:
        logger.debug("Theme file {} cannot be looked up: {}", path, error)
        return False


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
    if not path.is_relative_to(folder) or not _is_existing_file(path):
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
    path = _theme_file_path(entry, relative)
    if path.suffix.lower() != STYLE_FILE_SUFFIX:
        return ThemeFile(path=path)
    # utf-8-sig drops a leading byte-order mark, as a browser's decoding and the validator do.
    css_text = path.read_text(encoding="utf-8-sig")
    served = style_text_to_serve(css_text, str(theme_id), posixpath.dirname(relative))
    if served == css_text:
        return ThemeFile(path=path)
    return ThemeStyleFile(path=path, css=served)


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

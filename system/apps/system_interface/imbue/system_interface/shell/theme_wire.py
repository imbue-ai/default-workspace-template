"""The theme catalog on the wire (docs/system/blueprint/workspace-themes/, section 5.2): the body of
``GET /api/themes`` and of a ``themes_changed`` message."""

from typing import Any
from typing import Final
from urllib.parse import quote

from pydantic import Field
from workspace_themes.contract import FALLBACK_ICON_STEM
from workspace_themes.data_types import ResolvedChrome
from workspace_themes.data_types import ThemeCatalog
from workspace_themes.data_types import ThemeEntry

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import ThemeChoices

THEME_ICON_ROUTE: Final[str] = "/api/themes/{theme_id}/icons/{file_name}"


@pure
def _icon_url(theme_id: str, file_name: str, revision: str) -> str:
    return f"{THEME_ICON_ROUTE.format(theme_id=quote(theme_id), file_name=quote(file_name))}?v={revision}"


class ThemeIconsWire(FrozenModel):
    """A theme's icon limits and where each of its icons is served."""

    format: str = Field(description="svg or png")
    size: int = Field(description="The icon's edge in pixels")
    rendering: str = Field(description="How the shell scales the icon")
    background: str = Field(description="Whether the corners must be clear")
    palette: list[str] = Field(description="The colors the theme's icons are drawn from")
    max_colors: int | None = Field(description="How many colors an icon may use; null for no limit")
    derive: str = Field(description="How an app the theme draws no icon for gets one")
    fallback_url: str | None = Field(description="The theme's generic program icon, when it has one")
    apps: dict[str, str] = Field(description="Each app the theme has an icon for, and the icon's URL")


class ThemeWire(FrozenModel):
    """One theme: what the picker shows, what the title bar draws, and where the icons are."""

    id: str = Field(description="The theme's id")
    name: str = Field(description="What the picker calls it")
    description: str = Field(description="One line on its look")
    base: str | None = Field(description="The theme it builds on; null for one that builds on none")
    source: str = Field(description="Built in or made in this workspace")
    available: bool = Field(description="Whether it can be worn: it validates and its base chain resolves")
    problems: list[str] = Field(description="Why it cannot be worn, when it cannot")
    revision: str = Field(description="Changes whenever any of its files does")
    chrome: ResolvedChrome | None = Field(description="How the title bar is drawn, for a theme that can be worn")
    icons: ThemeIconsWire | None = Field(
        description="Its icons, for an available theme with icons of its own; the icon route serves no other"
    )


class ThemeCatalogWire(FrozenModel):
    """The body of ``GET /api/themes`` and of a ``themes_changed`` message."""

    default: str | None = Field(description="The workspace's default theme; null wears the standard one")
    themes: list[ThemeWire] = Field(description="Every theme folder the workspace has")


@pure
def theme_wire(entry: ThemeEntry) -> ThemeWire:
    """One theme of ``GET /api/themes`` (none of the icons for an unavailable theme, whose icons the icon route does
    not serve)."""
    manifest = entry.manifest
    icons: ThemeIconsWire | None = None
    if entry.icons is not None and entry.is_available:
        spec = entry.icons.spec
        suffix = f".{spec.format}"
        app_names = sorted({*entry.icons.curated_by_app, *entry.icons.generated_by_app})
        icons = ThemeIconsWire(
            format=str(spec.format),
            size=spec.size,
            rendering=str(spec.rendering),
            background=str(spec.background),
            palette=[str(color) for color in spec.palette],
            max_colors=spec.max_colors,
            derive=str(spec.derive),
            fallback_url=None
            if entry.icons.fallback is None
            else _icon_url(entry.id, f"{FALLBACK_ICON_STEM}{suffix}", entry.revision),
            apps={str(name): _icon_url(entry.id, f"{name}{suffix}", entry.revision) for name in app_names},
        )
    return ThemeWire(
        id=str(entry.id),
        name=str(manifest.name) if manifest is not None else str(entry.id),
        description=str(manifest.description) if manifest is not None else "",
        base=entry.chain[-2] if len(entry.chain) > 1 else None,
        source=str(entry.source),
        available=entry.is_available,
        problems=list(entry.problems),
        revision=str(entry.revision),
        chrome=entry.chrome,
        icons=icons,
    )


@pure
def theme_catalog_wire(catalog: ThemeCatalog, choices: ThemeChoices) -> ThemeCatalogWire:
    """The body of ``GET /api/themes`` and of a ``themes_changed`` message."""
    return ThemeCatalogWire(
        default=None if choices.default is None else str(choices.default),
        themes=[theme_wire(entry) for entry in catalog.entries],
    )


@pure
def theme_catalog_wire_json(catalog: ThemeCatalog, choices: ThemeChoices) -> dict[str, Any]:
    """``theme_catalog_wire`` as the JSON a ``themes_changed`` message carries."""
    return theme_catalog_wire(catalog, choices).model_dump(mode="json")

"""The theme catalog on the wire (docs/system/blueprint/workspace-themes/, section 5.2): the body of
``GET /api/themes`` and of a ``themes_changed`` message."""

from typing import Any
from typing import Final
from urllib.parse import quote

from workspace_themes.contract import FALLBACK_ICON_STEM
from workspace_themes.data_types import ThemeCatalog
from workspace_themes.data_types import ThemeEntry

from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import ThemeChoices

THEME_ICON_ROUTE: Final[str] = "/api/themes/{theme_id}/icons/{file_name}"


@pure
def _icon_url(theme_id: str, file_name: str, revision: str) -> str:
    return f"{THEME_ICON_ROUTE.format(theme_id=quote(theme_id), file_name=quote(file_name))}?v={revision}"


@pure
def theme_wire_json(entry: ThemeEntry) -> dict[str, Any]:
    """One theme of ``GET /api/themes``: what the picker shows, what the title bar draws, and where the icons are
    (none for an unavailable theme, whose icons the icon route does not serve)."""
    manifest = entry.manifest
    icons_json: dict[str, Any] | None = None
    if entry.icons is not None and entry.is_available:
        spec = entry.icons.spec
        suffix = f".{spec.format}"
        app_names = sorted({*entry.icons.curated_by_app, *entry.icons.generated_by_app})
        icons_json = {
            "format": str(spec.format),
            "size": spec.size,
            "rendering": str(spec.rendering),
            "background": str(spec.background),
            "palette": [str(color) for color in spec.palette],
            "max_colors": spec.max_colors,
            "derive": str(spec.derive),
            "fallback_url": None
            if entry.icons.fallback is None
            else _icon_url(entry.id, f"{FALLBACK_ICON_STEM}{suffix}", entry.revision),
            "apps": {str(name): _icon_url(entry.id, f"{name}{suffix}", entry.revision) for name in app_names},
        }
    return {
        "id": str(entry.id),
        "name": str(manifest.name) if manifest is not None else str(entry.id),
        "description": str(manifest.description) if manifest is not None else "",
        "base": entry.chain[-2] if len(entry.chain) > 1 else None,
        "source": str(entry.source),
        "available": entry.is_available,
        "problems": list(entry.problems),
        "revision": str(entry.revision),
        "chrome": None if entry.chrome is None else entry.chrome.model_dump(mode="json"),
        "icons": icons_json,
    }


@pure
def theme_catalog_wire_json(catalog: ThemeCatalog, choices: ThemeChoices) -> dict[str, Any]:
    """The body of ``GET /api/themes`` and of a ``themes_changed`` message."""
    return {
        "default": None if choices.default is None else str(choices.default),
        "themes": [theme_wire_json(entry) for entry in catalog.entries],
    }

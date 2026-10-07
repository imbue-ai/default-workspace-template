from pathlib import Path

from workspace_themes.catalog import CachingThemeCatalogLoader
from workspace_themes.primitives import ThemeSource

# system/libs/workspace_themes/src/workspace_themes/test_builtin_themes.py -> the repo root
_REPO_ROOT = Path(__file__).resolve().parents[5]
_BUILTIN_THEME_IDS = ("standard", "mac-classic", "windows-2000")
_BUILTIN_APPS_WITH_RETRO_ICONS = (
    "browser",
    "chat",
    "files",
    "getting-started",
    "terminal",
)


def test_every_built_in_theme_passes_the_contract() -> None:
    catalog = CachingThemeCatalogLoader(repo_root=_REPO_ROOT).load()

    for theme_id in _BUILTIN_THEME_IDS:
        entry = catalog.find(theme_id)
        assert entry is not None, f"{theme_id} is missing from system/themes/"
        assert entry.source == ThemeSource.BUILTIN
        assert entry.problems == (), f"{theme_id}: {entry.problems}"


def test_the_retro_themes_carry_an_icon_for_every_built_in_app() -> None:
    catalog = CachingThemeCatalogLoader(repo_root=_REPO_ROOT).load()

    for theme_id in ("mac-classic", "windows-2000"):
        entry = catalog.find_available(theme_id)
        assert entry is not None and entry.icons is not None
        assert set(_BUILTIN_APPS_WITH_RETRO_ICONS) <= set(entry.icons.curated_by_app), (
            theme_id
        )
        assert entry.icons.fallback is not None

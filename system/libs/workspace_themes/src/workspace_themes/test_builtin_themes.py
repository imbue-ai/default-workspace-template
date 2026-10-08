from pathlib import Path

from workspace_themes.catalog import CachingThemeCatalogLoader, read_app_manifests
from workspace_themes.contract import STANDARD_THEME_ID
from workspace_themes.primitives import ThemeSource

# system/libs/workspace_themes/src/workspace_themes/test_builtin_themes.py -> the repo root
_REPO_ROOT = Path(__file__).resolve().parents[5]
_BUILTIN_THEME_IDS = ("standard", "mac-classic", "windows-2000")


def test_every_built_in_theme_passes_the_contract() -> None:
    catalog = CachingThemeCatalogLoader(repo_root=_REPO_ROOT).load()

    for theme_id in _BUILTIN_THEME_IDS:
        entry = catalog.find(theme_id)
        assert entry is not None, f"{theme_id} is missing from system/themes/"
        assert entry.source == ThemeSource.BUILTIN
        assert entry.problems == (), f"{theme_id}: {entry.problems}"


def test_every_built_in_theme_with_its_own_icons_ships_one_for_every_built_in_app() -> (
    None
):
    """Every app a user sees (not internal) has an icon drawn, and committed, in every built-in theme that draws
    its own; a built-in app added later fails here until each such theme has one."""
    catalog = CachingThemeCatalogLoader(repo_root=_REPO_ROOT).load()
    standard = catalog.find(STANDARD_THEME_ID)
    assert standard is not None
    app_names = {
        str(manifest.name)
        for manifest in read_app_manifests(_REPO_ROOT)
        if not manifest.internal
    }
    assert app_names, "no built-in apps were found"

    themes_with_own_icons = [
        entry
        for entry in catalog.entries
        if entry.source == ThemeSource.BUILTIN
        and entry.icons is not None
        and entry.icons.spec_folder != standard.folder
    ]
    assert {str(entry.id) for entry in themes_with_own_icons} == {
        "mac-classic",
        "windows-2000",
    }
    for entry in themes_with_own_icons:
        assert entry.icons is not None
        missing = sorted(app_names - set(entry.icons.curated_by_app))
        assert missing == [], f"{entry.id} ships no icon for {missing}"
        assert entry.icons.fallback is not None, entry.id

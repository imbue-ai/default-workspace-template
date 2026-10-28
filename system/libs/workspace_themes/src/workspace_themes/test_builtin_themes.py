from pathlib import Path

from workspace_themes.catalog import CachingThemeCatalogLoader, read_app_manifests
from workspace_themes.contract import STANDARD_THEME_ID
from workspace_themes.primitives import ThemeSource

# system/libs/workspace_themes/src/workspace_themes/test_builtin_themes.py -> the repo root
_REPO_ROOT = Path(__file__).resolve().parents[5]
_BUILTIN_THEME_IDS = ("standard", "mac-classic", "windows-2000")
# The apps a user sees that the template ships. Only these need a built-in theme's icon: a workspace
# built from the template may carry user-built apps, whose icon the theme derives from the app's own,
# and this suite runs there too.
_BUILT_IN_VISIBLE_APP_NAMES = frozenset(
    {
        "browser",
        "chat",
        "datalib",
        "files",
        "getting-started",
        "routines",
        "system-monitor",
        "terminal",
    }
)


def test_every_built_in_theme_passes_the_contract() -> None:
    catalog = CachingThemeCatalogLoader(repo_root=_REPO_ROOT).load()

    builtin_entries = [
        entry for entry in catalog.entries if entry.source == ThemeSource.BUILTIN
    ]
    assert set(_BUILTIN_THEME_IDS) <= {str(entry.id) for entry in builtin_entries}
    for entry in builtin_entries:
        assert entry.problems == (), f"{entry.id}: {entry.problems}"


def test_every_built_in_theme_with_its_own_icons_ships_one_for_every_built_in_app() -> (
    None
):
    """Every built-in app a user sees has an icon drawn, and committed, in every built-in theme that draws its own."""
    catalog = CachingThemeCatalogLoader(repo_root=_REPO_ROOT).load()
    standard = catalog.find(STANDARD_THEME_ID)
    assert standard is not None
    visible_app_names = {
        str(manifest.name)
        for manifest in read_app_manifests(_REPO_ROOT)
        if not manifest.internal
    }
    unknown = sorted(_BUILT_IN_VISIBLE_APP_NAMES - visible_app_names)
    assert unknown == [], f"no visible app manifest for the built-in apps {unknown}"

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
        missing = sorted(_BUILT_IN_VISIBLE_APP_NAMES - set(entry.icons.curated_by_app))
        assert missing == [], f"{entry.id} ships no icon for {missing}"
        assert entry.icons.fallback is not None, entry.id

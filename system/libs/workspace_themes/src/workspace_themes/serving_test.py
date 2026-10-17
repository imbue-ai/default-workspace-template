from pathlib import Path

import pytest

from workspace_themes.catalog import default_theme_roots, read_theme_catalog
from workspace_themes.contract import (
    BUILTIN_THEMES_DIRECTORY,
    GENERATED_ICONS_DIRECTORY,
)
from workspace_themes.data_types import ThemeCatalog
from workspace_themes.errors import ThemeFileNotFoundError
from workspace_themes.serving import (
    ThemeBundle,
    ThemeFile,
    ThemeStyleFile,
    resolve_theme_asset,
    resolve_theme_icon,
    theme_file_security_headers,
)
from workspace_themes.testing import (
    write_standard_theme,
    write_test_icon,
    write_test_theme,
)


@pytest.fixture
def catalog(tmp_path: Path) -> ThemeCatalog:
    write_standard_theme(tmp_path)
    write_test_theme(
        tmp_path / BUILTIN_THEMES_DIRECTORY,
        "paper",
        "",
        app_names_with_icons=("files",),
    )
    write_test_icon(
        tmp_path / GENERATED_ICONS_DIRECTORY / "paper" / "icons" / "notes.png", 32
    )
    return read_theme_catalog(
        default_theme_roots(tmp_path), {}, tmp_path / GENERATED_ICONS_DIRECTORY
    )


def test_the_bundle_of_the_standard_theme_imports_nothing(
    catalog: ThemeCatalog,
) -> None:
    asset = resolve_theme_asset(catalog, "standard/theme.css")

    assert isinstance(asset, ThemeBundle)
    assert "@import" not in asset.css


def test_a_served_file_is_a_style_or_asset_file_inside_its_theme_folder(
    catalog: ThemeCatalog,
) -> None:
    asset = resolve_theme_asset(catalog, "paper/icons/files.png")

    assert isinstance(asset, ThemeFile) and asset.path.name == "files.png"


@pytest.mark.parametrize(
    "request_path",
    [
        "paper/theme.toml",
        "paper/icons/guide.md",
        "paper/../standard/theme.toml",
        "paper/%2e%2e/paper/parts.css",
        "paper/a\x00.css",
        "paper/missing.css",
        f"paper/{'a' * 300}.css",
        "unknown/theme.css",
        "Paper/theme.css",
        "paper",
    ],
)
def test_anything_else_is_not_found(catalog: ThemeCatalog, request_path: str) -> None:
    with pytest.raises(ThemeFileNotFoundError):
        resolve_theme_asset(catalog, request_path)


def test_an_icon_is_curated_or_generated_or_the_generic_program_icon(
    catalog: ThemeCatalog,
) -> None:
    assert resolve_theme_icon(catalog, "paper", "files.png").parent.name == "icons"
    assert GENERATED_ICONS_DIRECTORY.split("/")[-1] in str(
        resolve_theme_icon(catalog, "paper", "notes.png")
    )
    assert resolve_theme_icon(catalog, "paper", "app.png").name == "app.png"
    with pytest.raises(ThemeFileNotFoundError):
        resolve_theme_icon(catalog, "paper", "terminal.png")
    with pytest.raises(ThemeFileNotFoundError):
        resolve_theme_icon(catalog, "paper", "files.svg")


def test_an_icon_replaced_by_a_link_after_the_catalog_was_read_is_not_served(
    tmp_path: Path, catalog: ThemeCatalog
) -> None:
    icon = tmp_path / BUILTIN_THEMES_DIRECTORY / "paper" / "icons" / "files.png"
    outside = tmp_path / "secret.png"
    write_test_icon(outside, 32)
    icon.unlink()
    icon.symlink_to(outside)

    with pytest.raises(ThemeFileNotFoundError):
        resolve_theme_icon(catalog, "paper", "files.png")


def test_a_style_file_replaced_by_a_link_after_the_catalog_was_read_is_not_served(
    tmp_path: Path, catalog: ThemeCatalog
) -> None:
    style_file = tmp_path / BUILTIN_THEMES_DIRECTORY / "paper" / "parts.css"
    outside = tmp_path / "secret.css"
    outside.write_text("secret { color: red; }\n", encoding="utf-8")
    assert isinstance(resolve_theme_asset(catalog, "paper/parts.css"), ThemeFile)
    style_file.unlink()
    style_file.symlink_to(outside)

    with pytest.raises(ThemeFileNotFoundError):
        resolve_theme_asset(catalog, "paper/parts.css")


def test_a_theme_file_is_never_sniffed_and_an_svg_runs_nothing() -> None:
    assert theme_file_security_headers(Path("icons/files.png")) == {
        "X-Content-Type-Options": "nosniff"
    }
    assert theme_file_security_headers(Path("icons/files.SVG")) == {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    }


_BUBBLES_CSS = """:root {
  --desk-default-wallpaper: url("assets/bubbles.svg");
}
@media (min-width: 1px) {
  :root { --theme-tile: image-set("assets/bubbles.svg" 1x), url(data:image/png;base64,AAAA); }
}
[data-part="desktop"] {
  background-image: url("assets/bubbles.svg");
}
"""


def _bubbles_catalog(tmp_path: Path) -> ThemeCatalog:
    write_standard_theme(tmp_path)
    folder = write_test_theme(
        tmp_path / BUILTIN_THEMES_DIRECTORY, "bubbles", _BUBBLES_CSS
    )
    (folder / "assets").mkdir()
    (folder / "assets" / "bubbles.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"/>\n',
        encoding="utf-8",
    )
    catalog = read_theme_catalog(
        default_theme_roots(tmp_path), {}, tmp_path / GENERATED_ICONS_DIRECTORY
    )
    assert catalog.find_available("bubbles") is not None
    return catalog


def test_a_relative_url_in_a_custom_property_is_sent_as_the_theme_file_s_own_path(
    tmp_path: Path,
) -> None:
    asset = resolve_theme_asset(_bubbles_catalog(tmp_path), "bubbles/parts.css")

    assert isinstance(asset, ThemeStyleFile)
    assert (
        '--desk-default-wallpaper: url("/_static/themes/bubbles/assets/bubbles.svg");'
        in asset.css
    )
    assert 'image-set("/_static/themes/bubbles/assets/bubbles.svg" 1x)' in asset.css
    assert "url(data:image/png;base64,AAAA)" in asset.css
    assert 'background-image: url("assets/bubbles.svg");' in asset.css


def test_a_style_file_with_no_relative_url_in_a_custom_property_is_sent_as_it_is(
    catalog: ThemeCatalog,
) -> None:
    assert isinstance(resolve_theme_asset(catalog, "paper/parts.css"), ThemeFile)

from pathlib import Path

from app_manifest.primitives import AppName
from flask import Flask

from workspace_themes.catalog import default_theme_roots, read_theme_catalog
from workspace_themes.contract import (
    BUILTIN_THEMES_DIRECTORY,
    GENERATED_ICONS_DIRECTORY,
    THEME_MANIFEST_FILENAME,
)
from workspace_themes.data_types import ThemeCatalog
from workspace_themes.flask_routes import register_theme_static_route
from workspace_themes.testing import write_standard_theme, write_test_theme


def test_a_bundle_is_fetched_afresh_when_an_app_it_styles_appears_under_the_same_revision(
    tmp_path: Path,
) -> None:
    write_standard_theme(tmp_path)
    folder = write_test_theme(tmp_path / BUILTIN_THEMES_DIRECTORY, "paper", "")
    manifest = folder / THEME_MANIFEST_FILENAME
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            'files = ["parts.css"]\n', 'files = ["parts.css"]\napps = ["notes"]\n'
        ),
        encoding="utf-8",
    )
    (folder / "apps").mkdir()
    (folder / "apps" / "notes.css").write_text("", encoding="utf-8")

    def catalog_with(app_names: tuple[str, ...]) -> ThemeCatalog:
        return read_theme_catalog(
            default_theme_roots(tmp_path),
            {AppName(name): frozenset() for name in app_names},
            tmp_path / GENERATED_ICONS_DIRECTORY,
        )

    without_app = catalog_with(())
    with_app = catalog_with(("notes",))
    served = [without_app]
    application = Flask(__name__)
    register_theme_static_route(application, lambda: served[0])
    client = application.test_client()

    before = client.get("/_static/themes/paper/theme.css")
    served[0] = with_app
    after = client.get(
        "/_static/themes/paper/theme.css",
        headers={"If-None-Match": before.headers["ETag"]},
    )

    paper_without_app = without_app.find("paper")
    paper_with_app = with_app.find("paper")
    assert paper_without_app is not None and paper_with_app is not None
    assert paper_without_app.revision == paper_with_app.revision
    assert b"apps/notes.css" not in before.data
    assert after.status_code == 200 and b"apps/notes.css" in after.data


def test_theme_files_are_served_with_their_security_headers(tmp_path: Path) -> None:
    write_standard_theme(tmp_path)
    folder = write_test_theme(tmp_path / BUILTIN_THEMES_DIRECTORY, "paper", "")
    (folder / "assets").mkdir()
    (folder / "assets" / "mark.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"/>', encoding="utf-8"
    )
    catalog = read_theme_catalog(
        default_theme_roots(tmp_path), {}, tmp_path / GENERATED_ICONS_DIRECTORY
    )
    application = Flask(__name__)
    register_theme_static_route(application, lambda: catalog)
    client = application.test_client()

    bundle = client.get("/_static/themes/paper/theme.css")
    style = client.get("/_static/themes/paper/parts.css")
    svg = client.get("/_static/themes/paper/assets/mark.svg")

    assert bundle.status_code == style.status_code == svg.status_code == 200
    assert bundle.headers["X-Content-Type-Options"] == "nosniff"
    assert style.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" not in style.headers
    assert svg.headers["X-Content-Type-Options"] == "nosniff"
    assert svg.headers["Content-Security-Policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    )

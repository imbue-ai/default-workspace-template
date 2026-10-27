from pathlib import Path

import pytest
from flask.testing import FlaskClient

from app_schema.testing import assert_api_schema_describes_app
from style_guide_app.server import create_app
from workspace_themes.testing import write_standard_theme

MANIFEST_PATH = Path(__file__).resolve().parents[2] / "app.toml"


@pytest.fixture
def static_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "static"
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_text("<!doctype html><title>Style Guide</title><div id=app></div>")
    (directory / "assets" / "index-AbCdEf12.js").write_text("console.log('bundle');")
    (directory / "assets" / "logo.svg").write_text("<svg/>")
    return directory


@pytest.fixture
def client(static_directory: Path) -> FlaskClient:
    return create_app(static_directory).test_client()


def test_the_api_document_describes_every_route(client: FlaskClient) -> None:
    assert_api_schema_describes_app(client, MANIFEST_PATH)


def test_the_page_serves_the_built_document_uncached(client: FlaskClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "<div id=app></div>" in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"
    assert "Content-Security-Policy" in response.headers


def test_a_content_named_asset_is_cached_for_good_and_another_is_revalidated(client: FlaskClient) -> None:
    hashed = client.get("/assets/index-AbCdEf12.js")
    assert hashed.status_code == 200
    assert "max-age=31536000" in hashed.headers["Cache-Control"]
    plain = client.get("/assets/logo.svg")
    assert plain.status_code == 200
    assert plain.headers["Cache-Control"] == "no-cache"
    assert client.get("/assets/missing.js").status_code == 404


def test_an_unbuilt_page_says_how_to_build_it_and_health_reports_it(tmp_path: Path) -> None:
    client = create_app(tmp_path / "never-built").test_client()
    page = client.get("/")
    assert page.status_code == 200
    assert "npm run build" in page.get_data(as_text=True)
    assert client.get("/api/health").get_json() == {"status": "ok", "is_frontend_built": False}


def test_health_reports_a_built_page(client: FlaskClient) -> None:
    assert client.get("/api/health").get_json() == {"status": "ok", "is_frontend_built": True}


def test_the_workspace_theme_is_served_from_the_pages_own_origin(
    static_directory: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_standard_theme(tmp_path)
    monkeypatch.chdir(tmp_path)

    response = create_app(static_directory).test_client().get("/_static/themes/standard/theme.css")

    assert response.status_code == 200 and response.mimetype == "text/css"
    assert create_app(static_directory).test_client().get("/_static/themes/no-such-theme/theme.css").status_code == 404


def test_the_page_kit_sample_is_a_plain_page_loading_the_kit(client: FlaskClient) -> None:
    response = client.get("/page-kit-sample")
    page = response.get_data(as_text=True)
    assert response.status_code == 200 and response.mimetype == "text/html"
    assert '<link rel="stylesheet" href="/_static/workspace_theme.css" />' in page
    assert '<script src="/_static/workspace_theme.js"></script>' in page
    assert "Content-Security-Policy" in response.headers


def test_the_page_kit_files_are_served_from_the_shells_build_and_nothing_else_is(
    static_directory: Path, tmp_path: Path
) -> None:
    kit = tmp_path / "shell-static"
    kit.mkdir()
    (kit / "workspace_theme.js").write_text("/* kit */")
    (kit / "workspace_theme.css").write_text(":root {}")
    (kit / "app_contract.js").write_text("/* not the kit */")
    client = create_app(static_directory, shell_static_directory=kit).test_client()

    script = client.get("/_static/workspace_theme.js")
    assert script.status_code == 200 and script.mimetype == "text/javascript"
    assert script.get_data(as_text=True) == "/* kit */"
    assert client.get("/_static/workspace_theme.css").mimetype == "text/css"
    assert client.get("/_static/app_contract.js").status_code == 404
    assert create_app(static_directory, shell_static_directory=tmp_path / "unbuilt").test_client().get(
        "/_static/workspace_theme.js"
    ).status_code == 404
